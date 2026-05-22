"""Ingestion service orchestrator."""

import logging
import pathlib
import shutil
import sqlite3
from typing import Sequence

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.notes_checklist import BuildNotesChecklistRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.types import (
    ActiveAudit, BuildNoteItemDraft, SourceFileCategory, 
    SourceFileDraft, ThtChecklistItemDraft
)

from . import categorizer
from . import cross_validation
from . import gatekeeper
from . import hashing
from .errors import FileStorageError
from .parsers import audit_bom, coordinate_map, eco_build_notes, traveler

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit_repo: AuditRepository,
        source_file_repo: SourceFileRepository,
        tht_repo: ThtChecklistRepository,
        notes_repo: BuildNotesChecklistRepository,
        coord_map: coordinate_map.TravelerCoordinateMap,
        file_storage_root: pathlib.Path
    ) -> None:
        self.conn = conn
        self.audit_repo = audit_repo
        self.source_file_repo = source_file_repo
        self.tht_repo = tht_repo
        self.notes_repo = notes_repo
        self.coord_map = coord_map
        self.file_storage_root = file_storage_root

    def ingest(self, paths: Sequence[pathlib.Path]) -> ActiveAudit:
        """Ingest a dropped trio, atomically persist, and return the new audit."""
        
        gatekeeper.validate(paths)
        trio = categorizer.categorize(paths)
        
        bom_hash = hashing.sha256_hex(trio.bom_path)
        trav_hash = hashing.sha256_hex(trio.traveler_path)
        notes_hash = hashing.sha256_hex(trio.notes_path)

        # Parse part number early from BOM name to construct storage path
        # In case of mismatch, cross_validation will catch it, but we need
        # a directory name now.
        temp_part_number = trio.bom_path.name.split()[0].strip()
        audit_dir = self.file_storage_root / temp_part_number / "unsplit"

        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise FileStorageError(audit_dir, audit_dir, e)

        stored_bom = audit_dir / trio.bom_path.name
        stored_trav = audit_dir / trio.traveler_path.name
        stored_notes = audit_dir / trio.notes_path.name

        copied_paths = []
        try:
            for src, dst in [
                (trio.bom_path, stored_bom), 
                (trio.traveler_path, stored_trav), 
                (trio.notes_path, stored_notes)
            ]:
                if not dst.exists():
                    shutil.copy2(src, dst)
                    copied_paths.append(dst)
        except Exception as e:
            for cp in copied_paths:
                try:
                    cp.unlink()
                except Exception:
                    pass
            # src is where it failed, dst is where it was trying to go
            raise FileStorageError(src, dst, e)

        # Parse from stored locations
        try:
            bom_result = audit_bom.parse(stored_bom)
            eco_result = eco_build_notes.parse(stored_notes)
            trav_result = traveler.parse(stored_trav, self.coord_map)
            intent = cross_validation.reconcile(bom_result, eco_result, trav_result, self.coord_map)
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

            if intent.bom_items:
                self.tht_repo.insert_many([
                    ThtChecklistItemDraft(
                        audit_id=audit.id, source_file_id=bom_file.id,
                        component_mpn=item.component_mpn, description=item.description
                    ) for item in intent.bom_items
                ])

            if intent.eco_items:
                self.notes_repo.insert_many([
                    BuildNoteItemDraft(
                        audit_id=audit.id, source_file_id=notes_file.id,
                        row_sequence=item.row_sequence, original_text=item.original_text
                    ) for item in intent.eco_items
                ])

            self.conn.execute("RELEASE SAVEPOINT ingest")
            return audit
            
        except Exception as e:
            self.conn.execute("ROLLBACK TO SAVEPOINT ingest")
            self.conn.execute("RELEASE SAVEPOINT ingest")
            
            for cp, file_hash in [
                (stored_bom, bom_hash), 
                (stored_trav, trav_hash), 
                (stored_notes, notes_hash)
            ]:
                if cp in copied_paths:
                    if self.source_file_repo.reference_count(file_hash) == 0:
                        try:
                            cp.unlink()
                        except Exception as unlink_e:
                            logger.warning(f"Failed to delete {cp} during rollback: {unlink_e}")
                    else:
                        logger.info(f"Skipping deletion of {cp} during rollback; referenced by sibling.")
            
            raise
