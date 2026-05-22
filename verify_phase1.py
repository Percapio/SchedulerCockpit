import os
import pathlib
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path

from cockpit.persistence import (
    open_connection, migrate_to_v1,
    AuditRepository, SourceFileRepository,
    ThtChecklistRepository, BuildNotesChecklistRepository
)
from cockpit.persistence.types import *
from cockpit.persistence.errors import *


class TestPhase1(unittest.TestCase):
    def setUp(self):
        self.db_path = Path("test_db.sqlite")
        if self.db_path.exists():
            self.db_path.unlink()
        
        self.conn = open_connection(self.db_path)
        migrate_to_v1(self.conn)

    def tearDown(self):
        self.conn.close()
        if self.db_path.exists():
            self.db_path.unlink()

    def test_1_roundtrip_and_cascade(self):
        audits = AuditRepository(self.conn)
        files = SourceFileRepository(self.conn)
        tht = ThtChecklistRepository(self.conn)
        notes = BuildNotesChecklistRepository(self.conn)

        # 1. Create audit
        audit = audits.create(ActiveAuditDraft(
            part_number="PN-123",
            work_order_ref="WO-456",
            quantity=10,
            split_suffix=""
        ))
        
        # 2. Register two source files
        file1 = files.register(SourceFileDraft(
            audit_id=audit.id,
            file_category=SourceFileCategory.BOM,
            original_filename="bom.xlsx",
            local_storage_path=Path("/tmp/bom.xlsx"),
            file_hash="a" * 64
        ))
        file2 = files.register(SourceFileDraft(
            audit_id=audit.id,
            file_category=SourceFileCategory.NOTES,
            original_filename="notes.docx",
            local_storage_path=Path("/tmp/notes.docx"),
            file_hash="b" * 64
        ))

        # 3. Insert THT items
        tht_items = tht.insert_many([
            ThtChecklistItemDraft(audit_id=audit.id, component_mpn="C1", source_file_id=file1.id),
            ThtChecklistItemDraft(audit_id=audit.id, component_mpn="C2", source_file_id=file1.id),
        ])

        # 4. Insert Build Notes items
        notes_items = notes.insert_many([
            BuildNoteItemDraft(audit_id=audit.id, row_sequence=1, original_text="Step 1", source_file_id=file2.id),
            BuildNoteItemDraft(audit_id=audit.id, row_sequence=2, original_text="Step 2", source_file_id=file2.id),
        ])

        # 5. Flip every checklist row to verified
        for item in tht_items:
            tht.set_verification(item.id, True, None)
        for item in notes_items:
            notes.set_verification(item.id, True, None)

        # 6. Transition to IN_PROGRESS
        audit = audits.transition_status(audit.id, AuditStatus.IN_PROGRESS)
        self.assertEqual(audit.status, AuditStatus.IN_PROGRESS)

        # 7. Transition to COMPLETED
        audit = audits.transition_status(audit.id, AuditStatus.COMPLETED)
        self.assertEqual(audit.status, AuditStatus.COMPLETED)

        # 8. hard_delete
        audits.hard_delete(audit.id)

        # Assert zero rows in every table
        cur = self.conn.cursor()
        for table in ["active_audits", "source_files", "tht_verification_checklist", "build_notes_checklist"]:
            cur.execute(f"SELECT COUNT(*) as c FROM {table}")
            count = cur.fetchone()["c"]
            self.assertEqual(count, 0, f"Table {table} should be empty")

    def test_2_composite_identity_unique(self):
        audits = AuditRepository(self.conn)
        draft = ActiveAuditDraft(part_number="PN", work_order_ref="WO", quantity=5, split_suffix="-A")
        audits.create(draft)
        
        with self.assertRaises(DuplicateIdentityError):
            audits.create(draft)

    def test_3_state_machine(self):
        audits = AuditRepository(self.conn)
        audit = audits.create(ActiveAuditDraft(part_number="PN", work_order_ref="WO", quantity=5))
        
        # Pending -> Completed is illegal
        with self.assertRaises(IllegalStateTransition):
            audits.transition_status(audit.id, AuditStatus.COMPLETED)

        # Pending -> InProgress is OK
        audit = audits.transition_status(audit.id, AuditStatus.IN_PROGRESS)

        # InProgress -> Pending is OK
        audit = audits.transition_status(audit.id, AuditStatus.PENDING)

    def test_4_completion_guard(self):
        audits = AuditRepository(self.conn)
        tht = ThtChecklistRepository(self.conn)

        audit = audits.create(ActiveAuditDraft(part_number="PN", work_order_ref="WO", quantity=5))
        tht.insert_many([ThtChecklistItemDraft(audit_id=audit.id, component_mpn="C1")])

        audits.transition_status(audit.id, AuditStatus.IN_PROGRESS)

        with self.assertRaises(IncompleteChecklistError):
            audits.transition_status(audit.id, AuditStatus.COMPLETED)
            
        audit = audits.find_by_id(audit.id)
        self.assertEqual(audit.status, AuditStatus.IN_PROGRESS)

    def test_5_foreign_keys_pragma(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        
        audits = AuditRepository(self.conn)
        files = SourceFileRepository(self.conn)
        
        audit = audits.create(ActiveAuditDraft(part_number="PN", work_order_ref="WO", quantity=5))
        file = files.register(SourceFileDraft(
            audit_id=audit.id, file_category=SourceFileCategory.BOM, original_filename="a",
            local_storage_path=Path("a"), file_hash="a"*64
        ))
        
        audits.hard_delete(audit.id)
        
        # File should be orphaned
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM source_files")
        self.assertEqual(cur.fetchone()["c"], 1)

    def test_6_reference_count(self):
        audits = AuditRepository(self.conn)
        files = SourceFileRepository(self.conn)
        
        audit1 = audits.create(ActiveAuditDraft(part_number="PN1", work_order_ref="WO1", quantity=5))
        audit2 = audits.create(ActiveAuditDraft(part_number="PN2", work_order_ref="WO2", quantity=5))
        
        hash_val = "a" * 64
        files.register(SourceFileDraft(
            audit_id=audit1.id, file_category=SourceFileCategory.BOM, original_filename="a",
            local_storage_path=Path("a"), file_hash=hash_val
        ))
        files.register(SourceFileDraft(
            audit_id=audit2.id, file_category=SourceFileCategory.BOM, original_filename="a",
            local_storage_path=Path("a"), file_hash=hash_val
        ))
        
        self.assertEqual(files.reference_count(hash_val), 2)
        
        audits.hard_delete(audit1.id)
        self.assertEqual(files.reference_count(hash_val), 1)
        
        self.assertEqual(files.reference_count("b" * 64), 0)

    def test_7_migrate_idempotency_and_mismatch(self):
        # 1. Double invoke
        migrate_to_v1(self.conn) # Should no-op
        
        # 2. Mismatch
        cur = self.conn.cursor()
        cur.execute("UPDATE schema_version SET version = 2")
        
        with self.assertRaises(SchemaMismatch):
            migrate_to_v1(self.conn)

    def test_8_hard_delete_idempotency(self):
        audits = AuditRepository(self.conn)
        # Should not raise
        audits.hard_delete(9999)

if __name__ == '__main__':
    unittest.main()
