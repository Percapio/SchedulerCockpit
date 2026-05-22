"""Audit repository implementation."""

import json
import sqlite3
from typing import Any

from ..clock import utcnow
from ..errors import (
    AuditNotFound,
    DuplicateIdentityError,
    IllegalStateTransition,
    IncompleteChecklistError,
    InvalidArgumentError,
)
from ..types import ActiveAudit, ActiveAuditDraft, AuditStatus


class AuditRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, draft: ActiveAuditDraft) -> ActiveAudit:
        if not draft.part_number:
            raise InvalidArgumentError("part_number", draft.part_number, "Cannot be empty")
        if not draft.work_order_ref:
            raise InvalidArgumentError("work_order_ref", draft.work_order_ref, "Cannot be empty")
        if draft.split_suffix is None:
            raise InvalidArgumentError("split_suffix", draft.split_suffix, "Cannot be None (use '')")
        
        try:
            traveler_json = json.dumps(draft.traveler_metadata) if draft.traveler_metadata is not None else None
        except TypeError:
            raise InvalidArgumentError("traveler_metadata", draft.traveler_metadata, "Must be JSON serializable")

        now_iso = utcnow().isoformat()
        cur = self.conn.cursor()

        try:
            cur.execute(
                """
                INSERT INTO active_audits (
                    part_number, schedule_job_id, work_order_ref, split_suffix,
                    quantity, status, traveler_metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.part_number,
                    draft.schedule_job_id,
                    draft.work_order_ref,
                    draft.split_suffix,
                    draft.quantity,
                    AuditStatus.PENDING,
                    traveler_json,
                    now_iso,
                    now_iso
                )
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                raise DuplicateIdentityError(draft.part_number, draft.work_order_ref, draft.split_suffix) from e
            raise

        audit_id = cur.lastrowid
        assert audit_id is not None
        
        return self.find_by_id(audit_id)  # type: ignore

    def find_by_identity(
        self,
        part_number: str,
        work_order_ref: str,
        split_suffix: str = "",
    ) -> ActiveAudit | None:
        if split_suffix is None:
            raise InvalidArgumentError("split_suffix", split_suffix, "Cannot be None")

        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM active_audits 
            WHERE part_number = ? AND work_order_ref = ? AND split_suffix = ?
            """,
            (part_number, work_order_ref, split_suffix)
        )
        row = cur.fetchone()
        if not row:
            return None
        return ActiveAudit(**row)

    def find_by_id(self, audit_id: int) -> ActiveAudit | None:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM active_audits WHERE id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            return None
        return ActiveAudit(**row)

    def transition_status(self, audit_id: int, target: AuditStatus) -> ActiveAudit:
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("SELECT status FROM active_audits WHERE id = ?", (audit_id,))
            row = cur.fetchone()
            if not row:
                raise AuditNotFound(audit_id)

            current_status = AuditStatus(row["status"])

            # Validate transition per state diagram
            allowed = False
            if current_status == AuditStatus.PENDING and target == AuditStatus.IN_PROGRESS:
                allowed = True
            elif current_status == AuditStatus.IN_PROGRESS and target == AuditStatus.PENDING:
                allowed = True
            elif current_status == AuditStatus.IN_PROGRESS and target == AuditStatus.COMPLETED:
                allowed = True
            
            if not allowed:
                raise IllegalStateTransition(audit_id, current_status, target)

            if target == AuditStatus.COMPLETED:
                cur.execute(
                    "SELECT COUNT(*) as unverified FROM tht_verification_checklist WHERE audit_id = ? AND is_verified = 0",
                    (audit_id,)
                )
                tht_unverified = cur.fetchone()["unverified"]

                cur.execute(
                    "SELECT COUNT(*) as unverified FROM build_notes_checklist WHERE audit_id = ? AND is_verified = 0",
                    (audit_id,)
                )
                notes_unverified = cur.fetchone()["unverified"]

                if tht_unverified > 0 or notes_unverified > 0:
                    raise IncompleteChecklistError(audit_id, tht_unverified, notes_unverified)

            now_iso = utcnow().isoformat()
            cur.execute(
                "UPDATE active_audits SET status = ?, updated_at = ? WHERE id = ?",
                (target, now_iso, audit_id)
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

        return self.find_by_id(audit_id)  # type: ignore

    def set_split_reason(self, audit_id: int, reason: str) -> None:
        if not reason or not reason.strip():
            raise InvalidArgumentError("reason", reason, "Cannot be blank")
            
        cur = self.conn.cursor()
        cur.execute("UPDATE active_audits SET split_reason = ? WHERE id = ?", (reason.strip(), audit_id))
        if cur.rowcount == 0:
            raise AuditNotFound(audit_id)

    def set_traveler_metadata(self, audit_id: int, payload: dict[str, Any] | None) -> None:
        try:
            traveler_json = json.dumps(payload) if payload is not None else None
        except TypeError:
            raise InvalidArgumentError("payload", payload, "Must be JSON serializable")

        cur = self.conn.cursor()
        cur.execute("UPDATE active_audits SET traveler_metadata = ? WHERE id = ?", (traveler_json, audit_id))
        if cur.rowcount == 0:
            raise AuditNotFound(audit_id)

    def hard_delete(self, audit_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM active_audits WHERE id = ?", (audit_id,))
