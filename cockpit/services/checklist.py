"""Checklist service."""

import logging
from cockpit.persistence.errors import AuditNotFound, ChecklistItemNotFound
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.repositories.notes_checklist import BuildNotesChecklistRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.types import AuditStatus, SourceFileCategory

from cockpit.services.views import (
    ActiveAuditView,
    ChecklistRowKind,
    ChecklistRowKey,
    ChecklistRowView,
)


import sqlite3

logger = logging.getLogger(__name__)

class ChecklistService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit_repo: AuditRepository,
        tht_repo: ThtChecklistRepository,
        notes_repo: BuildNotesChecklistRepository,
        source_file_repo: SourceFileRepository,
    ) -> None:
        self._conn = conn
        self._audit_repo = audit_repo
        self._tht_repo = tht_repo
        self._notes_repo = notes_repo
        self._source_file_repo = source_file_repo

    def load_active_audit(self, audit_id: int) -> ActiveAuditView:
        audit = self._audit_repo.find_by_id(audit_id)
        if audit is None:
            raise AuditNotFound(audit_id)

        tht_rows_db = self._tht_repo.list_for_audit(audit_id)
        notes_rows_db = self._notes_repo.list_for_audit(audit_id)

        tht_views = [
            ChecklistRowView(
                key=ChecklistRowKey(ChecklistRowKind.THT, r.id),
                primary_label=r.component_mpn,
                secondary_label=r.description,
                is_verified=r.is_verified
            ) for r in tht_rows_db
        ]

        notes_views = [
            ChecklistRowView(
                key=ChecklistRowKey(ChecklistRowKind.NOTES, r.id),
                primary_label=f"{r.row_sequence}.",
                secondary_label=r.original_text,
                is_verified=r.is_verified
            ) for r in notes_rows_db
        ]

        has_pdf = self._source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF) is not None

        return ActiveAuditView(
            audit_id=audit.id,
            part_number=audit.part_number,
            work_order_ref=audit.work_order_ref,
            split_suffix=audit.split_suffix,
            quantity=audit.quantity,
            status=audit.status,
            split_reason=audit.split_reason,
            traveler_metadata=audit.traveler_metadata,
            ship_date=audit.ship_date,
            has_pdf=has_pdf,
            tht_rows=tht_views,
            notes_rows=notes_views,
            general_notes=audit.general_notes,
        )

    def set_verification(
        self,
        row_key: ChecklistRowKey,
        is_verified: bool,
    ) -> ChecklistRowView:
        if row_key.kind == ChecklistRowKind.THT:
            r = self._tht_repo.set_verification(row_key.item_id, is_verified)
            return ChecklistRowView(
                key=row_key,
                primary_label=r.component_mpn,
                secondary_label=r.description,
                is_verified=r.is_verified
            )
        else:
            r = self._notes_repo.set_verification(row_key.item_id, is_verified)
            return ChecklistRowView(
                key=row_key,
                primary_label=f"{r.row_sequence}.",
                secondary_label=r.original_text,
                is_verified=r.is_verified
            )

    def complete(self, audit_id: int) -> ActiveAuditView:
        self._audit_repo.transition_status(audit_id, AuditStatus.COMPLETED)
        return self.load_active_audit(audit_id)

    def verify_all(self, audit_id: int) -> ActiveAuditView:
        self._conn.execute("SAVEPOINT verify_all")
        try:
            self._tht_repo.mark_all_verified(audit_id)
            self._notes_repo.mark_all_verified(audit_id)
            self._conn.execute("RELEASE SAVEPOINT verify_all")
        except Exception:
            logger.exception("Suppressed Exception in verify_all")
            self._conn.execute("ROLLBACK TO SAVEPOINT verify_all")
            self._conn.execute("RELEASE SAVEPOINT verify_all")
            raise
            
        return self.load_active_audit(audit_id)
