"""Repository for audit_requirements and X-ray tick persistence (Phase 54 §5.2, §5.3, §8)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Callable

from cockpit.persistence.clock import utcnow
from cockpit.services.requirements import (
    FLAGGABLE_KINDS,
    AuditNotFoundForTick,
    Failure,
    HandFlag,
    HandFlagChange,
    HandFlagRefused,
    LocationsNotRead,
    NoOpenAudit,
    NotFlaggable,
    MAX_EVIDENCE,
    StoredXray,
    XrayLocationsReading,
    xray_locations_from_json,
    xray_locations_to_json,
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
                xray_task, evidence, unknown_reasons, fingerprint, scanned_at, xray_locations
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id) DO UPDATE SET
                xray_save_images = excluded.xray_save_images,
                xray             = excluded.xray,
                press_fit        = excluded.press_fit,
                aquanox          = excluded.aquanox,
                xray_task        = excluded.xray_task,
                evidence         = excluded.evidence,
                unknown_reasons  = excluded.unknown_reasons,
                fingerprint      = excluded.fingerprint,
                scanned_at       = excluded.scanned_at,
                xray_locations   = excluded.xray_locations
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
                xray_locations_to_json(scan.xray_locations),
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

    def find_stored_xray(self, audit_id: int) -> StoredXray | None:
        """Phase 58 §4.3: the requirements row's part of what X-ray mode shows for one audit.

        post: None iff the audit has no audit_requirements row; otherwise reading is LocationsNotRead
              when the column is NULL, LocationsUnreadable when it doesn't parse or names a newer
              format, and StoredLocations otherwise
        """
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT xray_save_images, xray, unknown_reasons, fingerprint, xray_locations
            FROM audit_requirements
            WHERE audit_id = ?
            """,
            (audit_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        raw_reasons = row["unknown_reasons"]
        reasons_dict = json.loads(raw_reasons) if isinstance(raw_reasons, str) else (raw_reasons or {})
        unknown_reasons: dict[RequirementKind, str] = {}
        for kind, reason in reasons_dict.items():
            try:
                unknown_reasons[RequirementKind(kind)] = str(reason)
            except ValueError:
                continue
        return StoredXray(
            reading=xray_locations_from_json(row["xray_locations"]),
            save_images=_db_to_state(row["xray_save_images"]),
            xray=_db_to_state(row["xray"]),
            unknown_reasons=unknown_reasons,
            fingerprint=row["fingerprint"],
        )

    def find_xray_reading(self, audit_id: int) -> XrayLocationsReading:
        """The stored locations alone, for a scan's prior (§8.5): not read when there is no row."""
        cur = self.conn.cursor()
        cur.execute("SELECT xray_locations FROM audit_requirements WHERE audit_id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            return LocationsNotRead()
        return xray_locations_from_json(row["xray_locations"])

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
                    xray_task, evidence, unknown_reasons, fingerprint, scanned_at, xray_locations
                )
                SELECT
                    ?, xray_save_images, xray, press_fit, aquanox,
                    xray_task, evidence, unknown_reasons, fingerprint, scanned_at, xray_locations
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
                    scanned_at       = excluded.scanned_at,
                    xray_locations   = excluded.xray_locations
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

    # ---- Phase 59 §4.3: hand flags, per part number -------------------------

    def set_hand_flag(
        self,
        part_number: str,
        kind: RequirementKind,
        flagged: bool,
        clock: Callable[[], datetime] = utcnow,
    ) -> Result[HandFlagChange, HandFlagRefused]:
        """Sets or clears one hand flag for every open audit of a part number (Phase 59 §4.3).

        pre:  UI thread; outside any ingest savepoint
        post: flagged: Success iff some open audit has part_number; the row then exists, keeping its
              first flagged_at when it was already there and taking clock() otherwise;
              Failure(NoOpenAudit) when none has it, with nothing written. Not flagged: Success, and
              no row remains. Failure(NotFlaggable) for a kind outside FLAGGABLE_KINDS. The open-audit
              check and the insert are one statement, so a completion can't land between them.
        raises: sqlite3.Error
        """
        if kind not in FLAGGABLE_KINDS:
            return Failure(NotFlaggable(kind))
        cur = self.conn.cursor()
        if not flagged:
            cur.execute(
                "DELETE FROM part_requirement_flags WHERE part_number = ? AND kind = ?",
                (part_number, kind.value),
            )
            return Success(HandFlagChange(part_number, kind, None))

        now_dt = clock()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        cur.execute(
            """
            INSERT INTO part_requirement_flags (part_number, kind, flagged_at)
            SELECT ?, ?, ?
            WHERE EXISTS (SELECT 1 FROM active_audits WHERE part_number = ?)
            ON CONFLICT (part_number, kind) DO NOTHING
            """,
            (part_number, kind.value, now_dt.isoformat(), part_number),
        )
        if cur.rowcount == 1:
            return Success(HandFlagChange(part_number, kind, HandFlag(kind, part_number, now_dt)))
        cur.execute(
            """
            SELECT flagged_at AS flagged_raw FROM part_requirement_flags
            WHERE part_number = ? AND kind = ?
              AND EXISTS (SELECT 1 FROM active_audits WHERE part_number = ?)
            """,
            (part_number, kind.value, part_number),
        )
        row = cur.fetchone()
        if row is None:
            return Failure(NoOpenAudit(part_number))
        try:
            flagged_at = _parse_dt(row["flagged_raw"]) or now_dt
        except ValueError:
            flagged_at = now_dt
        return Success(HandFlagChange(part_number, kind, HandFlag(kind, part_number, flagged_at)))

    def hand_flags_by_part_number(self) -> dict[str, dict[RequirementKind, HandFlag]]:
        """Every hand flag, by part number, for the Audit List's one load (Phase 59 §4.3).

        post: an entry per part number with at least one flag; a row of a kind this build doesn't
              know is skipped; a database without the table reads as none
        raises: sqlite3.Error
        """
        rows = self._flag_rows("SELECT part_number, kind, flagged_at AS flagged_raw FROM part_requirement_flags", ())
        flags: dict[str, dict[RequirementKind, HandFlag]] = {}
        for part_number, flag in rows:
            flags.setdefault(part_number, {})[flag.kind] = flag
        return flags

    def hand_flags_for(self, part_number: str) -> dict[RequirementKind, HandFlag]:
        """One part number's hand flags, for the Audit View and the update preview (Phase 59 §4.3).

        raises: sqlite3.Error
        """
        rows = self._flag_rows(
            "SELECT part_number, kind, flagged_at AS flagged_raw FROM part_requirement_flags WHERE part_number = ?",
            (part_number,),
        )
        return {flag.kind: flag for _part_number, flag in rows}

    def delete_orphan_hand_flags(self) -> int:
        """Deletes the flags whose part number no open audit has (Phase 59 §4.3).

        pre:  inside the caller's savepoint when called from completion or Reset
        post: every remaining row's part_number is some active_audits row's; returns how many went
        raises: sqlite3.Error
        """
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                DELETE FROM part_requirement_flags
                WHERE NOT EXISTS (
                    SELECT 1 FROM active_audits a WHERE a.part_number = part_requirement_flags.part_number
                )
                """
            )
        except sqlite3.OperationalError as exc:
            if "no such table: part_requirement_flags" not in str(exc):
                raise
            return 0
        return max(cur.rowcount, 0)

    def _flag_rows(self, sql: str, params: tuple) -> list[tuple[str, HandFlag]]:
        """Rows as (part number, flag). flagged_at is read under an alias, flagged_raw, so the
        connection's row factory, which parses every *_at column, can't fail the whole read on one bad
        row; a row whose time or kind doesn't parse is skipped alone."""
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "no such table: part_requirement_flags" not in str(exc):
                raise
            return []
        out: list[tuple[str, HandFlag]] = []
        for row in cur.fetchall():
            try:
                kind = RequirementKind(row["kind"])
            except ValueError:
                continue
            if kind not in FLAGGABLE_KINDS:
                continue
            try:
                flagged_at = _parse_dt(row["flagged_raw"])
            except ValueError:
                continue
            if flagged_at is None:
                continue
            out.append((row["part_number"], HandFlag(kind, row["part_number"], flagged_at)))
        return out


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
