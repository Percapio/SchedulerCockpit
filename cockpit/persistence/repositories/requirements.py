"""Repository for audit_requirements and X-ray tick persistence (Phase 54 §5.2, §5.3, §8)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Callable

from cockpit.persistence.clock import utcnow
from cockpit.services.requirements import (
    AuditNotFoundForTick,
    Failure,
    MAX_EVIDENCE,
    RequirementEvidence,
    RequirementKind,
    RequirementScan,
    RequirementState,
    RequirementSummary,
    Result,
    SaveImagesNotRequired,
    Success,
    TickRefused,
    UnitSource,
    XrayTick,
)


def _state_to_db(state: RequirementState) -> int | None:
    if state == RequirementState.DETECTED:
        return 1
    if state == RequirementState.ABSENT:
        return 0
    return None


def _db_to_state(val: int | None) -> RequirementState:
    if val == 1:
        return RequirementState.DETECTED
    if val == 0:
        return RequirementState.ABSENT
    return RequirementState.UNKNOWN


def _parse_dt(val: str | datetime | None) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    dt = datetime.fromisoformat(str(val))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_summary(row: dict | sqlite3.Row) -> RequirementSummary:
    raw_reasons = row["unknown_reasons"]
    reasons_dict = json.loads(raw_reasons) if isinstance(raw_reasons, str) else (raw_reasons or {})
    unknown_reasons: dict[RequirementKind, str] = {}
    for k, v in reasons_dict.items():
        try:
            unknown_reasons[RequirementKind(k)] = str(v)
        except ValueError:
            continue

    scanned_dt = _parse_dt(row["scanned_at"])
    assert scanned_dt is not None

    return RequirementSummary(
        states={
            RequirementKind.XRAY_SAVE_IMAGES: _db_to_state(row["xray_save_images"]),
            RequirementKind.XRAY: _db_to_state(row["xray"]),
            RequirementKind.PRESS_FIT: _db_to_state(row["press_fit"]),
            RequirementKind.AQUANOX: _db_to_state(row["aquanox"]),
        },
        unknown_reasons=unknown_reasons,
        xray_task=row["xray_task"],
        fingerprint=row["fingerprint"],
        scanned_at=scanned_dt,
    )


class AuditRequirementsRepository:
    """Persistence access for audit_requirements and active_audits X-ray tick columns."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert_scan(
        self,
        audit_id: int,
        scan: RequirementScan,
        scanned_at: datetime | None = None,
    ) -> None:
        """Inserts or replaces the scan row for audit_id inside the caller's transaction (§5.2)."""
        now_iso = (scanned_at or utcnow()).isoformat()
        ev_payload = [
            {
                "excerpt": ev.excerpt,
                "is_negated": bool(ev.is_negated),
                "kind": ev.kind.value,
                "location": ev.location,
                "source": ev.source.value,
            }
            for ev in scan.evidence[:MAX_EVIDENCE]
        ]
        reasons_payload = {
            k.value: v for k, v in scan.unknown_reasons.items()
        }
        xray_save_db = _state_to_db(scan.states[RequirementKind.XRAY_SAVE_IMAGES])
        xray_db = _state_to_db(scan.states[RequirementKind.XRAY])
        pf_db = _state_to_db(scan.states[RequirementKind.PRESS_FIT])
        aq_db = _state_to_db(scan.states[RequirementKind.AQUANOX])
        xray_task_val = scan.xray_task if xray_save_db == 1 else None

        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO audit_requirements (
                audit_id, xray_save_images, xray, press_fit, aquanox,
                xray_task, evidence, unknown_reasons, fingerprint, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id) DO UPDATE SET
                xray_save_images = excluded.xray_save_images,
                xray             = excluded.xray,
                press_fit        = excluded.press_fit,
                aquanox          = excluded.aquanox,
                xray_task        = excluded.xray_task,
                evidence         = excluded.evidence,
                unknown_reasons  = excluded.unknown_reasons,
                fingerprint      = excluded.fingerprint,
                scanned_at       = excluded.scanned_at
            """,
            (
                audit_id,
                xray_save_db,
                xray_db,
                pf_db,
                aq_db,
                xray_task_val,
                json.dumps(ev_payload, ensure_ascii=False),
                json.dumps(reasons_payload, ensure_ascii=False),
                scan.fingerprint,
                now_iso,
            ),
        )

    def find_summary(self, audit_id: int) -> RequirementSummary | None:
        """Reads the RequirementSummary for one audit without loading evidence (§5.2)."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT audit_id, xray_save_images, xray, press_fit, aquanox,
                   xray_task, unknown_reasons, fingerprint, scanned_at
            FROM audit_requirements
            WHERE audit_id = ?
            """,
            (audit_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _row_to_summary(row)

    def find_summaries_for_open_audits(self) -> dict[int, RequirementSummary]:
        """Reads RequirementSummary for every audit in audit_requirements without evidence (§5.2)."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT audit_id, xray_save_images, xray, press_fit, aquanox,
                   xray_task, unknown_reasons, fingerprint, scanned_at
            FROM audit_requirements
            """
        )
        out: dict[int, RequirementSummary] = {}
        for row in cur.fetchall():
            out[int(row["audit_id"])] = _row_to_summary(row)
        return out

    def find_evidence(self, audit_id: int) -> list[RequirementEvidence]:
        """Reads the stored RequirementEvidence list for one audit on demand (§5.2, §9)."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT evidence FROM audit_requirements WHERE audit_id = ?",
            (audit_id,),
        )
        row = cur.fetchone()
        if not row or not row["evidence"]:
            return []
        raw_ev = row["evidence"]
        items = json.loads(raw_ev) if isinstance(raw_ev, str) else raw_ev
        result: list[RequirementEvidence] = []
        for item in items:
            try:
                result.append(
                    RequirementEvidence(
                        kind=RequirementKind(item["kind"]),
                        source=UnitSource(item["source"]),
                        location=str(item["location"]),
                        excerpt=str(item["excerpt"]),
                        is_negated=bool(item.get("is_negated", False)),
                    )
                )
            except (KeyError, ValueError):
                continue
        return result

    def clone_scan(self, source_audit_id: int, sibling_audit_id: int) -> None:
        """Copies the parent's audit_requirements row (if any) to sibling_audit_id (§5.3)."""
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO audit_requirements (
                    audit_id, xray_save_images, xray, press_fit, aquanox,
                    xray_task, evidence, unknown_reasons, fingerprint, scanned_at
                )
                SELECT
                    ?, xray_save_images, xray, press_fit, aquanox,
                    xray_task, evidence, unknown_reasons, fingerprint, scanned_at
                FROM audit_requirements
                WHERE audit_id = ?
                ON CONFLICT(audit_id) DO UPDATE SET
                    xray_save_images = excluded.xray_save_images,
                    xray             = excluded.xray,
                    press_fit        = excluded.press_fit,
                    aquanox          = excluded.aquanox,
                    xray_task        = excluded.xray_task,
                    evidence         = excluded.evidence,
                    unknown_reasons  = excluded.unknown_reasons,
                    fingerprint      = excluded.fingerprint,
                    scanned_at       = excluded.scanned_at
                """,
                (sibling_audit_id, source_audit_id),
            )
        except sqlite3.OperationalError as exc:
            if "no such table: audit_requirements" not in str(exc):
                raise

    def set_xray_images_done(
        self,
        audit_id: int,
        done: bool,
        clock: Callable[[], datetime] = utcnow,
    ) -> Result[XrayTick, TickRefused]:
        """Records or clears the X-ray & save images tick (§8)."""
        return set_xray_images_done(self.conn, audit_id, done, clock=clock)


_SAVE_IMAGES_DETECTED = (
    "EXISTS (SELECT 1 FROM audit_requirements WHERE audit_id = ? AND xray_save_images = 1)"
)


def set_xray_images_done(
    conn: sqlite3.Connection,
    audit_id: int,
    done: bool,
    clock: Callable[[], datetime] = utcnow,
) -> Result[XrayTick, TickRefused]:
    """Records or clears the X-ray & save images tick (§8).

    pre:  UI thread
    post: on Success with done, xray_images_done is 1, the task is the stored
          scan's xray_task and the time comes from clock; with not done, all
          three are cleared; Failure(AuditNotFoundForTick) when the audit is
          gone, Failure(SaveImagesNotRequired) when its stored scan doesn't say
          save-images is DETECTED, and nothing is written in either case

    The check and the write are one UPDATE, so a rescan can't slip between
    them, and no BEGIN IMMEDIATE is needed: an ingest holds its savepoint on a
    worker thread over this same connection, and a BEGIN inside it fails.
    """
    now_dt = clock()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = now_dt.isoformat()

    cur = conn.cursor()
    if done:
        cur.execute(
            f"""
            UPDATE active_audits
            SET xray_images_done = 1,
                xray_images_done_task = (SELECT xray_task FROM audit_requirements WHERE audit_id = ?),
                xray_images_done_at = ?,
                updated_at = ?
            WHERE id = ? AND {_SAVE_IMAGES_DETECTED}
            """,
            (audit_id, now_iso, now_iso, audit_id, audit_id),
        )
    else:
        cur.execute(
            f"""
            UPDATE active_audits
            SET xray_images_done = 0,
                xray_images_done_task = NULL,
                xray_images_done_at = NULL,
                updated_at = ?
            WHERE id = ? AND {_SAVE_IMAGES_DETECTED}
            """,
            (now_iso, audit_id, audit_id),
        )

    if cur.rowcount == 1:
        if not done:
            return Success(XrayTick(is_done=False, task_at_tick=None, ticked_at=None))
        cur.execute("SELECT xray_images_done_task FROM active_audits WHERE id = ?", (audit_id,))
        return Success(XrayTick(
            is_done=True, task_at_tick=cur.fetchone()["xray_images_done_task"], ticked_at=now_dt,
        ))

    cur.execute("SELECT id FROM active_audits WHERE id = ?", (audit_id,))
    if not cur.fetchone():
        return Failure(AuditNotFoundForTick(audit_id=audit_id))
    return Failure(SaveImagesNotRequired(audit_id=audit_id))
