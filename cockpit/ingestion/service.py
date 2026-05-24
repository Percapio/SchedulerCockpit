"""Ingestion service orchestrator."""

import logging
import pathlib
import shutil
import sqlite3
from typing import Sequence, Callable, Any

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.notes_checklist import BuildNotesChecklistRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.types import (
    ActiveAudit, AuditBomComponentDraft, BuildNoteItemDraft, PdfComponentCoordDraft,
    SourceFileCategory, SourceFileDraft, ThtChecklistItemDraft
)

from . import categorizer
from . import cross_validation
from . import gatekeeper
from . import hashing
from .errors import FileStorageError
from .parsers import audit_bom, coordinate_map, eco_build_notes, traveler
from .progress import ProgressEvent, ProgressStage

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit_repo: AuditRepository,
        source_file_repo: SourceFileRepository,
        tht_repo: ThtChecklistRepository,
        notes_repo: BuildNotesChecklistRepository,
        bom_component_repo: AuditBomComponentRepository,
        pdf_coord_repo: PdfComponentCoordRepository,
        layout_parser: Any,
        coord_map: coordinate_map.TravelerCoordinateMap,
        file_storage_root: pathlib.Path
    ) -> None:
        self.conn = conn
        self.audit_repo = audit_repo
        self.source_file_repo = source_file_repo
        self.tht_repo = tht_repo
        self.notes_repo = notes_repo
        self.bom_component_repo = bom_component_repo
        self.pdf_coord_repo = pdf_coord_repo
        self.layout_parser = layout_parser
        self.coord_map = coord_map
        self.file_storage_root = file_storage_root

    def ingest(self, paths: Sequence[pathlib.Path], progress: Callable[[ProgressEvent], None] | None = None) -> ActiveAudit:
        """Ingest a dropped trio, atomically persist, and return the new audit."""
        
        def _emit(stage: ProgressStage, detail: dict[str, Any] | None = None) -> None:
            if progress is not None:
                progress(ProgressEvent(stage=stage, detail=detail))

        gatekeeper.validate(paths)
        _emit(ProgressStage.GATEKEEPER_PASSED)

        quartet = categorizer.categorize(paths)
        _emit(ProgressStage.FILES_CATEGORIZED)
        
        bom_hash = hashing.sha256_hex(quartet.bom_path)
        trav_hash = hashing.sha256_hex(quartet.traveler_path)
        notes_hash = hashing.sha256_hex(quartet.notes_path)
        pdf_hash = hashing.sha256_hex(quartet.pdf_path) if quartet.pdf_path else None
        
        if pdf_hash:
            _emit(ProgressStage.PDF_HASHED)
        _emit(ProgressStage.FILES_HASHED)

        # Parse part number early from BOM name to construct storage path
        # In case of mismatch, cross_validation will catch it, but we need
        # a directory name now.
        temp_part_number = quartet.bom_path.name.split()[0].strip()
        audit_dir = self.file_storage_root / temp_part_number / "unsplit"

        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise FileStorageError(audit_dir, audit_dir, e)

        stored_bom = audit_dir / quartet.bom_path.name
        stored_trav = audit_dir / quartet.traveler_path.name
        stored_notes = audit_dir / quartet.notes_path.name
        stored_pdf = audit_dir / quartet.pdf_path.name if quartet.pdf_path else None

        copied_paths = []
        try:
            copy_targets = [
                (quartet.bom_path, stored_bom), 
                (quartet.traveler_path, stored_trav), 
                (quartet.notes_path, stored_notes)
            ]
            if quartet.pdf_path and stored_pdf:
                copy_targets.append((quartet.pdf_path, stored_pdf))

            for src, dst in copy_targets:
                if not dst.exists():
                    shutil.copy2(src, dst)
                    copied_paths.append(dst)
                    
            if quartet.pdf_path:
                _emit(ProgressStage.PDF_COPIED)
            _emit(ProgressStage.FILES_COPIED)
        except Exception as e:
            for cp in copied_paths:
                try:
                    cp.unlink()
                except Exception:
                    pass
            if isinstance(e, FileStorageError):
                raise
            raise FileStorageError(src, dst, e)

        # Parse from stored locations
        pdf_result = None
        try:
            bom_result = audit_bom.parse(stored_bom)
            _emit(ProgressStage.BOM_PARSED)

            eco_result = eco_build_notes.parse(stored_notes)
            _emit(ProgressStage.ECO_PARSED, {"eco_item_count": len(eco_result.items) if eco_result.items else 0})

            trav_result = traveler.parse(stored_trav, self.coord_map)
            _emit(ProgressStage.TRAVELER_PARSED)

            intent = cross_validation.reconcile(bom_result, eco_result, trav_result, self.coord_map)
            _emit(ProgressStage.CROSS_VALIDATED)
            
            if stored_pdf:
                # Gather all RefDes values across all BOM items
                target_ref_des = set()
                for item in intent.bom_items:
                    if item.ref_des_list:
                        target_ref_des.update(item.ref_des_list)
                        
                pdf_result = self.layout_parser.parse(stored_pdf, target_ref_des)
                _emit(ProgressStage.PDF_PARSED)
                
                # Log missing RefDes
                missing = target_ref_des - pdf_result.found_ref_des
                if missing:
                    logger.debug(f"PDF missing {len(missing)} RefDes components: {sorted(list(missing))}")
        except Exception:
            for cp in copied_paths:
                try:
                    cp.unlink()
                except Exception:
                    pass
            raise

        self.conn.execute("SAVEPOINT ingest")
        try:
            audit = self.audit_repo.create(intent.audit_draft)

            bom_file = self.source_file_repo.register(SourceFileDraft(
                audit_id=audit.id, file_category=SourceFileCategory.BOM,
                original_filename=stored_bom.name, local_storage_path=stored_bom, file_hash=bom_hash
            ))
            trav_file = self.source_file_repo.register(SourceFileDraft(
                audit_id=audit.id, file_category=SourceFileCategory.TRAVELER,
                original_filename=stored_trav.name, local_storage_path=stored_trav, file_hash=trav_hash
            ))
            notes_file = self.source_file_repo.register(SourceFileDraft(
                audit_id=audit.id, file_category=SourceFileCategory.NOTES,
                original_filename=stored_notes.name, local_storage_path=stored_notes, file_hash=notes_hash
            ))
            
            pdf_file = None
            if stored_pdf and pdf_hash:
                pdf_file = self.source_file_repo.register(SourceFileDraft(
                    audit_id=audit.id, file_category=SourceFileCategory.PDF,
                    original_filename=stored_pdf.name, local_storage_path=stored_pdf, file_hash=pdf_hash
                ))

            if intent.bom_items:
                # Insert side-table BOM components
                bom_drafts = []
                for item in intent.bom_items:
                    if item.ref_des_list:
                        for rd in item.ref_des_list:
                            bom_drafts.append(AuditBomComponentDraft(
                                source_file_id=bom_file.id, component_mpn=item.component_mpn,
                                ref_des=rd, mount_type=item.mount_type, description=item.description
                            ))
                if bom_drafts:
                    self.bom_component_repo.bulk_insert(bom_drafts)
                    
                # Insert THT checklist items (only where mount_type == 'T')
                self.tht_repo.insert_many([
                    ThtChecklistItemDraft(
                        audit_id=audit.id, source_file_id=bom_file.id,
                        component_mpn=item.component_mpn, description=item.description
                    ) for item in intent.bom_items if item.mount_type == 'T'
                ])
                
            if pdf_result and pdf_file:
                pdf_drafts = [
                    PdfComponentCoordDraft(
                        source_file_id=pdf_file.id, ref_des=c.ref_des, page_index=c.page_index,
                        x1=c.x1, y1=c.y1, x2=c.x2, y2=c.y2
                    ) for c in pdf_result.coordinates
                ]
                if pdf_drafts:
                    self.pdf_coord_repo.bulk_insert(pdf_drafts)

            if intent.eco_items:
                self.notes_repo.insert_many([
                    BuildNoteItemDraft(
                        audit_id=audit.id, source_file_id=notes_file.id,
                        row_sequence=item.row_sequence, original_text=item.original_text
                    ) for item in intent.eco_items
                ])

            _emit(ProgressStage.PERSISTED, {"tht_item_count": len(intent.bom_items) if intent.bom_items else 0})

            self.conn.execute("RELEASE SAVEPOINT ingest")
            return audit
            
        except Exception as e:
            self.conn.execute("ROLLBACK TO SAVEPOINT ingest")
            self.conn.execute("RELEASE SAVEPOINT ingest")
            
            rollback_targets = [
                (stored_bom, bom_hash), 
                (stored_trav, trav_hash), 
                (stored_notes, notes_hash)
            ]
            if stored_pdf and pdf_hash:
                rollback_targets.append((stored_pdf, pdf_hash))
                
            for cp, file_hash in rollback_targets:
                if cp in copied_paths:
                    if self.source_file_repo.reference_count(file_hash) == 0:
                        try:
                            cp.unlink()
                        except Exception as unlink_e:
                            logger.warning(f"Failed to delete {cp} during rollback: {unlink_e}")
                    else:
                        logger.info(f"Skipping deletion of {cp} during rollback; referenced by sibling.")
            
            raise
