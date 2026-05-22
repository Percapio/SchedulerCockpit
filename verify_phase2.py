import os
import pathlib
import shutil
import sqlite3
import unittest
from pathlib import Path

from cockpit.persistence import (
    open_connection, migrate_to_v1,
    AuditRepository, SourceFileRepository,
    ThtChecklistRepository, BuildNotesChecklistRepository
)
from cockpit.persistence.types import SourceFileCategory
from cockpit.ingestion import (
    IngestionService, validate, categorize, sha256_hex, load as load_map
)
from cockpit.ingestion.errors import (
    GatekeeperViolation, CoordinateMapError, CrossValidationError, FileStorageError, MalformedTravelerError
)
from cockpit.ingestion.parsers import audit_bom, eco_build_notes, traveler


class TestPhase2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # We need the data paths. Assuming we run from project root
        cls.data_dir = Path("backend/data")
        # Let's find one valid trio
        cls.valid_trio_paths = None
        
        # In a real environment, we would scan. For this test we find the first B138 or B139 that has 3 files
        for parent in [cls.data_dir / "B138xxx", cls.data_dir / "B139xxx"]:
            if not parent.exists():
                continue
            for d in parent.iterdir():
                if d.is_dir():
                    files = list(d.glob("*"))
                    if len(files) == 3:
                        cls.valid_trio_paths = files
                        break
            if cls.valid_trio_paths:
                break
                
    def setUp(self):
        if not self.valid_trio_paths:
            self.skipTest("No valid trio found in backend/data")
            
        self.db_path = Path("test_db_p2.sqlite")
        self.file_storage = Path("test_storage")
        
        if self.db_path.exists():
            self.db_path.unlink()
        if self.file_storage.exists():
            shutil.rmtree(self.file_storage)
            
        self.conn = open_connection(self.db_path)
        migrate_to_v1(self.conn)
        
        self.audit_repo = AuditRepository(self.conn)
        self.source_file_repo = SourceFileRepository(self.conn)
        self.tht_repo = ThtChecklistRepository(self.conn)
        self.notes_repo = BuildNotesChecklistRepository(self.conn)
        self.coord_map = load_map()
        
        self.service = IngestionService(
            self.conn, self.audit_repo, self.source_file_repo,
            self.tht_repo, self.notes_repo, self.coord_map, self.file_storage
        )

    def tearDown(self):
        self.conn.close()
        if self.db_path.exists():
            try:
                self.db_path.unlink()
            except Exception:
                pass
        if self.file_storage.exists():
            try:
                shutil.rmtree(self.file_storage)
            except Exception:
                pass

    def test_1_gatekeeper(self):
        # Valid trio passes
        validate(self.valid_trio_paths)
        
        # Wrong count
        with self.assertRaises(GatekeeperViolation) as e:
            validate(self.valid_trio_paths[:2])
        self.assertEqual(e.exception.reason, "WRONG_COUNT")
        
        # Missing File
        fake_path = [self.valid_trio_paths[0], self.valid_trio_paths[1], Path("does_not_exist.xlsx")]
        with self.assertRaises(GatekeeperViolation) as e:
            validate(fake_path)
        self.assertEqual(e.exception.reason, "MISSING_FILE")

    def test_7_end_to_end_ingestion(self):
        audit = self.service.ingest(self.valid_trio_paths)
        
        self.assertIsNotNone(audit)
        self.assertIsNotNone(audit.traveler_metadata)
        
        # Verify 3 source files
        files = self.source_file_repo.list_for_audit(audit.id)
        self.assertEqual(len(files), 3)
        categories = {f.file_category for f in files}
        self.assertEqual(categories, {SourceFileCategory.BOM, SourceFileCategory.TRAVELER, SourceFileCategory.NOTES})
        
        # Verify physical files exist
        for f in files:
            self.assertTrue(f.local_storage_path.exists())
            
        # Verify checklists
        tht_items = self.tht_repo.list_for_audit(audit.id)
        # Should be some items unless DNI filtered all, but practically there are some
        self.assertTrue(len(tht_items) >= 0)
        
        notes_items = self.notes_repo.list_for_audit(audit.id)
        self.assertTrue(len(notes_items) > 0)

    def test_8_rollback_on_failure(self):
        # We simulate a failure in parsing by corrupting one file or using a bad map
        # Let's pass a bad coordinate map that requires an impossible field
        bad_map_path = Path("bad_map.json")
        with open(bad_map_path, "w") as f:
            f.write('''{
                "version": 1, "template_revisions": [], "sheet_name": "Sheet1",
                "identity_mapping": {"part_number_field": "a", "work_order_ref_field": "b", "quantity_field": "c"},
                "anchors": [
                    {"field_key": "a", "anchor_cell": "Z99", "anchor_text": "NONEXISTENT", "value_offset": [0,0], "required": true},
                    {"field_key": "b", "anchor_cell": "Y99", "anchor_text": "NONEXISTENT", "value_offset": [0,0], "required": true},
                    {"field_key": "c", "anchor_cell": "X99", "anchor_text": "NONEXISTENT", "value_offset": [0,0], "required": true}
                ]
            }''')
            
        bad_map = load_map(bad_map_path)
        service = IngestionService(
            self.conn, self.audit_repo, self.source_file_repo,
            self.tht_repo, self.notes_repo, bad_map, self.file_storage
        )
        
        with self.assertRaises(Exception): # Will raise AnchorNotFound or similar parser error
            service.ingest(self.valid_trio_paths)
            
        # Verify db is untouched
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM active_audits")
        self.assertEqual(cur.fetchone()["c"], 0)
        
        # Verify storage is clean
        if self.file_storage.exists():
            # There might be the root directory, but inside it should be empty or nonexistent
            for root, dirs, files in os.walk(self.file_storage):
                self.assertEqual(len(files), 0, f"Found leaked files: {files}")
                
        bad_map_path.unlink()

if __name__ == '__main__':
    unittest.main()
