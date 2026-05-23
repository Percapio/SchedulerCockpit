"""Audit repository implementation."""

import json
import re
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
from ..types import ActiveAudit, ActiveAuditDraft, AuditStatus, SourceFileCategory

from .bom_components import AuditBomComponentRepository
from .pdf_coords import PdfComponentCoordRepository


class AuditRepository:
    def __init__(
        self,
        conn: sqlite3.Connection,
        bom_component_repo: AuditBomComponentRepository,
        pdf_coord_repo: PdfComponentCoordRepository,
    ) -> None:
        self.conn = conn
        self.bom_component_repo = bom_component_repo
        self.pdf_coord_repo = pdf_coord_repo

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

    def set_ship_date(self, audit_id: int, new_value: Any) -> ActiveAudit:
        from datetime import date, datetime
        if new_value is not None and not isinstance(new_value, date):
            raise InvalidArgumentError("new_value", new_value, "Must be date or None")
        if isinstance(new_value, datetime):
            raise InvalidArgumentError("new_value", new_value, "Must be date, not datetime")
            
        cur = self.conn.cursor()
        cur.execute("SELECT status FROM active_audits WHERE id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            raise AuditNotFound(audit_id)
            
        if row["status"] == AuditStatus.COMPLETED.value:
            raise IllegalStateTransition(audit_id, AuditStatus.COMPLETED, AuditStatus.COMPLETED)
            
        val = new_value.isoformat() if new_value is not None else None
        cur.execute("UPDATE active_audits SET ship_date = ? WHERE id = ?", (val, audit_id))
        return self.find_by_id(audit_id)  # type: ignore

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

    def list_open(self) -> list[ActiveAudit]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM active_audits
            WHERE status != ?
            ORDER BY updated_at DESC, created_at DESC
            """,
            (AuditStatus.COMPLETED.value,)
        )
        return [ActiveAudit(**row) for row in cur.fetchall()]

    def list_completed(self) -> list[ActiveAudit]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM active_audits
            WHERE status = ?
            ORDER BY created_at ASC
            """,
            (AuditStatus.COMPLETED.value,)
        )
        return [ActiveAudit(**row) for row in cur.fetchall()]

    def _validate_suffix_shape(self, suffix: str) -> None:
        if suffix is None:
            raise InvalidArgumentError("suffix", suffix, "Cannot be None")
        if not suffix.strip():
            raise InvalidArgumentError("suffix", suffix, "Cannot be empty after strip")
        if not suffix.startswith("-"):
            raise InvalidArgumentError("suffix", suffix, "Must start with '-'")
        if len(suffix) > 16:
            raise InvalidArgumentError("suffix", suffix, "Must be <= 16 chars")
        if not re.fullmatch(r"-[A-Za-z0-9]*", suffix):
            raise InvalidArgumentError("suffix", suffix, "Must contain only alphanumeric chars after '-'")

    def relabel_suffix(self, audit_id: int, new_suffix: str) -> ActiveAudit:
        self._validate_suffix_shape(new_suffix)
        
        cur = self.conn.cursor()
        cur.execute("SELECT status, part_number, work_order_ref FROM active_audits WHERE id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            raise AuditNotFound(audit_id)
            
        if row["status"] == AuditStatus.COMPLETED.value:
            raise IllegalStateTransition(audit_id, AuditStatus.COMPLETED, AuditStatus.COMPLETED)
            
        try:
            cur.execute(
                "UPDATE active_audits SET split_suffix = ? WHERE id = ?",
                (new_suffix, audit_id)
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                raise DuplicateIdentityError(row["part_number"], row["work_order_ref"], new_suffix) from e
            raise
            
        return self.find_by_id(audit_id)  # type: ignore

    def set_quantity(self, audit_id: int, new_quantity: int) -> ActiveAudit:
        if new_quantity < 1:
            raise InvalidArgumentError("new_quantity", new_quantity, "Must be >= 1")
            
        cur = self.conn.cursor()
        cur.execute("SELECT status FROM active_audits WHERE id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            raise AuditNotFound(audit_id)
            
        if row["status"] == AuditStatus.COMPLETED.value:
            raise IllegalStateTransition(audit_id, AuditStatus.COMPLETED, AuditStatus.COMPLETED)
            
        cur.execute("UPDATE active_audits SET quantity = ? WHERE id = ?", (new_quantity, audit_id))
        return self.find_by_id(audit_id)  # type: ignore

    def clone_to_suffix(
        self,
        source_audit_id: int,
        new_suffix: str,
        new_quantity: int,
        reason: str,
    ) -> ActiveAudit:
        self._validate_suffix_shape(new_suffix)
        if new_quantity < 1:
            raise InvalidArgumentError("new_quantity", new_quantity, "Must be >= 1")
        if not reason or not reason.strip():
            raise InvalidArgumentError("reason", reason, "Cannot be blank")
            
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM active_audits WHERE id = ?", (source_audit_id,))
        source = cur.fetchone()
        if not source:
            raise AuditNotFound(source_audit_id)
            
        if source["status"] == AuditStatus.COMPLETED.value:
            raise IllegalStateTransition(source_audit_id, AuditStatus.COMPLETED, AuditStatus.PENDING)
            
        now_iso = utcnow().isoformat()
        
        try:
            cur.execute(
                """
                INSERT INTO active_audits (
                    part_number, schedule_job_id, work_order_ref, split_suffix,
                    quantity, status, traveler_metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source["part_number"],
                    source["schedule_job_id"],
                    source["work_order_ref"],
                    new_suffix,
                    new_quantity,
                    AuditStatus.PENDING.value,
                    json.dumps(source["traveler_metadata"]) if source["traveler_metadata"] is not None else None,
                    now_iso,
                    now_iso
                )
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                raise DuplicateIdentityError(source["part_number"], source["work_order_ref"], new_suffix) from e
            raise
            
        sibling_id = cur.lastrowid
        assert sibling_id is not None
        
        # Clone source_files by reference (same local_storage_path and file_hash)
        cur.execute("SELECT * FROM source_files WHERE audit_id = ?", (source_audit_id,))
        source_files = cur.fetchall()
        
        file_id_map = {} # old_id -> new_id
        for sf in source_files:
            cur.execute(
                """
                INSERT INTO source_files (
                    audit_id, file_category, original_filename,
                    local_storage_path, file_hash, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sibling_id,
                    sf["file_category"],
                    sf["original_filename"],
                    str(sf["local_storage_path"]),
                    sf["file_hash"],
                    now_iso
                )
            )
            file_id_map[sf["id"]] = cur.lastrowid
            
            # Phase 7 side-table clones
            if sf["file_category"] == SourceFileCategory.BOM.value:
                self.bom_component_repo.clone_for_source_file(sf["id"], file_id_map[sf["id"]])
            elif sf["file_category"] == SourceFileCategory.PDF.value:
                self.pdf_coord_repo.clone_for_source_file(sf["id"], file_id_map[sf["id"]])
            
        # Clone THT rows
        cur.execute("SELECT * FROM tht_verification_checklist WHERE audit_id = ?", (source_audit_id,))
        tht_rows = cur.fetchall()
        for tr in tht_rows:
            cur.execute(
                """
                INSERT INTO tht_verification_checklist (
                    audit_id, source_file_id, component_mpn, description,
                    is_verified, notes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sibling_id,
                    file_id_map[tr["source_file_id"]],
                    tr["component_mpn"],
                    tr["description"],
                    0,
                    None
                )
            )
            
        # Clone Notes rows
        cur.execute("SELECT * FROM build_notes_checklist WHERE audit_id = ?", (source_audit_id,))
        notes_rows = cur.fetchall()
        for nr in notes_rows:
            cur.execute(
                """
                INSERT INTO build_notes_checklist (
                    audit_id, source_file_id, row_sequence, original_text,
                    is_verified, notes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sibling_id,
                    file_id_map[nr["source_file_id"]],
                    nr["row_sequence"],
                    nr["original_text"],
                    0,
                    None
                )
            )
            
        return self.find_by_id(sibling_id)  # type: ignore
