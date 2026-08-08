"""Checklist service."""

import logging
from cockpit.persistence.errors import AuditNotFound, ChecklistItemNotFound
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.repositories.notes_checklist import BuildNotesChecklistRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.types import AuditStatus, SourceFileCategory

from cockpit.services.views import (
    ActiveAuditView,
    ChecklistRowKind,
    ChecklistRowKey,
    ChecklistRowView,
)

import sqlite3

logger = logging.getLogger(__name__)

class RefDesIndexCache:
    def __init__(self, service: 'ChecklistService'):
        self._by_source_file: dict[int, dict[str, tuple[int, tuple[str, ...]]]] = {}
        self._service = service

    def get(self, source_file_id: int | None) -> dict[str, tuple[int, tuple[str, ...]]]:
        if source_file_id is None:
            return {}
        if source_file_id not in self._by_source_file:
            self._by_source_file[source_file_id] = self._service.build_tht_refdes_index(source_file_id)
        return self._by_source_file[source_file_id]

    def invalidate(self, source_file_id: int) -> None:
        self._by_source_file.pop(source_file_id, None)

    def clear(self) -> None:
        self._by_source_file.clear()

class BomSourceFileMemo:
    def __init__(self, source_file_repo: SourceFileRepository):
        self._by_audit: dict[int, int | None] = {}
        self._source_file_repo = source_file_repo

    def bom_source_file_id_for(self, audit_id: int) -> int | None:
        if audit_id not in self._by_audit:
            bom_sf = self._source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
            self._by_audit[audit_id] = bom_sf.id if bom_sf else None
        return self._by_audit[audit_id]

    def clear(self) -> None:
        self._by_audit.clear()

class ChecklistService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit_repo: AuditRepository,
        tht_repo: ThtChecklistRepository,
        notes_repo: BuildNotesChecklistRepository,
        source_file_repo: SourceFileRepository,
        bom_component_repo: AuditBomComponentRepository,
    ) -> None:
        self._conn = conn
        self._audit_repo = audit_repo
        self._tht_repo = tht_repo
        self._notes_repo = notes_repo
        self._source_file_repo = source_file_repo
        self._bom_component_repo = bom_component_repo
        self._refdes_index_cache = RefDesIndexCache(self)
        self._bom_source_file_memo = BomSourceFileMemo(self._source_file_repo)

    def build_tht_refdes_index(self, bom_sf_id: int | None) -> dict[str, tuple[int, tuple[str, ...]]]:
        if bom_sf_id is None:
            return {}
        
        bom_components = self._bom_component_repo.list_for_source_file(bom_sf_id)
        
        grouped = {}
        for c in bom_components:
            if c.mount_type != 'T':
                continue
            if c.component_mpn not in grouped:
                grouped[c.component_mpn] = {"find_number": c.find_number, "ref_des_list": []}
            grouped[c.component_mpn]["ref_des_list"].append(c.ref_des)
            
        index = {}
        for mpn, data in grouped.items():
            if not data["ref_des_list"]:
                continue
            index[mpn] = (data["find_number"], tuple(sorted(data["ref_des_list"])))
            
        return index

    def load_active_audit(self, audit_id: int) -> ActiveAuditView:
        audit = self._audit_repo.find_by_id(audit_id)
        if audit is None:
            raise AuditNotFound(audit_id)

        self._refdes_index_cache.clear()
        self._bom_source_file_memo.clear()

        source_files = self._source_file_repo.list_for_audit(audit_id)
        bom_sf = next((sf for sf in source_files if sf.file_category == SourceFileCategory.BOM.value), None)
        has_pdf = any(sf.file_category == SourceFileCategory.PDF.value for sf in source_files)
        has_secondary_pdf = any(sf.file_category == SourceFileCategory.SECONDARY_PDF.value for sf in source_files)
        
        bom_sf_id = bom_sf.id if bom_sf else None
        self._bom_source_file_memo._by_audit[audit_id] = bom_sf_id

        tht_index = self._refdes_index_cache.get(bom_sf_id)

        tht_rows_db = self._tht_repo.list_for_audit(audit_id)
        notes_rows_db = self._notes_repo.list_for_audit(audit_id)

        tht_views = []
        for r in tht_rows_db:
            idx_data = tht_index.get(r.component_mpn)
            find_number = idx_data[0] if idx_data else None
            ref_des_list = idx_data[1] if idx_data else ()
            
            tht_views.append(ChecklistRowView(
                key=ChecklistRowKey(ChecklistRowKind.THT, r.id),
                primary_label=r.component_mpn,
                secondary_label=r.description,
                is_verified=r.is_verified,
                find_number=find_number,
                ref_des_list=ref_des_list
            ))

        def sort_key(row_view: ChecklistRowView) -> tuple[bool, int, int]:
            return (row_view.find_number is None, row_view.find_number or 0, row_view.key.item_id)
            
        tht_views.sort(key=sort_key)

        notes_views = [
            ChecklistRowView(
                key=ChecklistRowKey(ChecklistRowKind.NOTES, r.id),
                primary_label=f"{r.row_sequence}.",
                secondary_label=r.original_text,
                is_verified=r.is_verified
            ) for r in notes_rows_db
        ]

        tht_placement_count: int = sum(len(ref_des_list) for _, ref_des_list in tht_index.values())

        return ActiveAuditView(
            audit_id=audit.id,
            part_number=audit.part_number,
            work_order_ref=audit.work_order_ref,
            split_suffix=audit.split_suffix,
            quantity=audit.quantity,
            status=audit.status,
            split_reason=audit.split_reason,
            traveler_metadata=audit.traveler_metadata,
            has_pdf=has_pdf,
            tht_placement_count=tht_placement_count,
            tht_rows=tht_views,
            notes_rows=notes_views,
            ship_date=audit.ship_date,
            ops_per_board_min=audit.ops_per_board_min,
            has_secondary_pdf=has_secondary_pdf,
            is_labeled=audit.is_labeled,
            are_photos_uploaded=audit.are_photos_uploaded,
        )

    def set_verification(
        self,
        row_key: ChecklistRowKey,
        is_verified: bool,
    ) -> ChecklistRowView:
        if row_key.kind == ChecklistRowKind.THT:
            r = self._tht_repo.set_verification(row_key.item_id, is_verified)
            
            bom_sf_id = self._bom_source_file_memo.bom_source_file_id_for(r.audit_id)
            tht_index = self._refdes_index_cache.get(bom_sf_id)
            idx_data = tht_index.get(r.component_mpn)
            
            return ChecklistRowView(
                key=row_key,
                primary_label=r.component_mpn,
                secondary_label=r.description,
                is_verified=r.is_verified,
                find_number=idx_data[0] if idx_data else None,
                ref_des_list=idx_data[1] if idx_data else ()
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

    def release_audit_scoped_caches(self) -> None:
        self._refdes_index_cache.clear()
        self._bom_source_file_memo.clear()
