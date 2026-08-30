import sqlite3
import pytest
from pathlib import Path
from cockpit.persistence.schema import migrate_to_v14
from cockpit.persistence.errors import SchemaMismatch, BackfillSourceMissing, MigrationError
from cockpit.ingestion.parsers.results import EcoResult, EcoItem, EcoImageRef

class DummyEcoParser:
    def __init__(self, responses=None):
        self.responses = responses or {}
    def parse(self, path: Path) -> EcoResult:
        if path in self.responses:
            return self.responses[path]
        return EcoResult(
            declared_part_number="ABC",
            items=[EcoItem(row_sequence=1, cells=("A", "B"), images=(), source_table_index=0)],
            raw_table_count=1
        )

class DummyRegistry:
    def __init__(self, eco_parser):
        self.eco_parser = eco_parser

def setup_v13_db(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("CREATE TABLE schema_version (singleton_guard INTEGER NOT NULL PRIMARY KEY CHECK (singleton_guard = 1), version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    cur.execute("INSERT INTO schema_version (singleton_guard, version, applied_at) VALUES (1, 13, '2023-01-01T00:00:00Z')")
    
    cur.execute("CREATE TABLE active_audits (id INTEGER PRIMARY KEY, part_number TEXT NOT NULL, schedule_job_id INTEGER, work_order_ref TEXT NOT NULL, split_suffix TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL, status TEXT NOT NULL, split_reason TEXT, traveler_metadata TEXT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, general_notes TEXT NULL, ship_date TEXT NULL, feeder_setuptime REAL NULL, smt_runtime REAL NULL, tht_runtime REAL NULL, aoi_runtime REAL NULL, ops_runtime REAL NULL, shipping_runtime REAL NULL, is_class_3 INTEGER NOT NULL DEFAULT 0, is_clean_process INTEGER NOT NULL DEFAULT 0, ops_per_board_min REAL NULL, is_labeled INTEGER NOT NULL DEFAULT 0, are_photos_uploaded INTEGER NOT NULL DEFAULT 0, UNIQUE(part_number, work_order_ref, split_suffix))")
    
    cur.execute("CREATE TABLE source_files (id INTEGER PRIMARY KEY, audit_id INTEGER NOT NULL, file_category TEXT NOT NULL, original_filename TEXT NOT NULL, local_storage_path TEXT NOT NULL, file_hash TEXT NOT NULL, ingested_at DATETIME NOT NULL, FOREIGN KEY(audit_id) REFERENCES active_audits(id) ON DELETE CASCADE)")
    
    cur.execute("CREATE TABLE build_notes_checklist (id INTEGER PRIMARY KEY, audit_id INTEGER NOT NULL, source_file_id INTEGER, row_sequence INTEGER NOT NULL, original_text TEXT NOT NULL, is_verified BOOLEAN NOT NULL DEFAULT 0, FOREIGN KEY(audit_id) REFERENCES active_audits(id) ON DELETE CASCADE, FOREIGN KEY(source_file_id) REFERENCES source_files(id) ON DELETE CASCADE)")
    
    conn.commit()

def test_v14_migration_preserves_verified_state(tmp_path):
    conn = sqlite3.connect(":memory:")
    setup_v13_db(conn)
    
    cur = conn.cursor()
    cur.execute("INSERT INTO active_audits (id, part_number, work_order_ref, quantity, status, created_at, updated_at) VALUES (1, 'A', 'W', 1, 'Not Clear', '2023', '2023')")
    notes_path = tmp_path / "notes.docx"
    notes_path.touch()
    cur.execute("INSERT INTO source_files (id, audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) VALUES (1, 1, 'Notes', 'notes.docx', ?, 'hash', '2023')", (str(notes_path),))
    
    cur.execute("INSERT INTO build_notes_checklist (audit_id, source_file_id, row_sequence, original_text, is_verified) VALUES (1, 1, 1, 'A / B', 1)")
    cur.execute("INSERT INTO build_notes_checklist (audit_id, source_file_id, row_sequence, original_text, is_verified) VALUES (1, 1, 2, 'C / D', 0)")
    
    res = EcoResult("A", [
        EcoItem(row_sequence=1, cells=("A", "B"), images=(), source_table_index=0),
        EcoItem(row_sequence=2, cells=("C", "D"), images=(), source_table_index=0)
    ], 1)
    registry = DummyRegistry(DummyEcoParser({notes_path: res}))
    conn.commit()
    assert migrate_to_v14(conn, registry) is True
    
    cur.execute("SELECT row_sequence, is_verified, cells FROM build_notes_checklist ORDER BY row_sequence")
    rows = [tuple(r) for r in cur.fetchall()]
    assert len(rows) == 2
    assert rows[0] == (1, 1, '["A", "B"]')
    assert rows[1] == (2, 0, '["C", "D"]')
    
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notes_media_cache'")
    assert cur.fetchone() is not None

def test_v14_missing_source_file(tmp_path):
    conn = sqlite3.connect(":memory:")
    setup_v13_db(conn)
    cur = conn.cursor()
    cur.execute("INSERT INTO active_audits (id, part_number, work_order_ref, quantity, status, created_at, updated_at) VALUES (1, 'A', 'W', 1, 'Not Clear', '2023', '2023')")
    notes_path = tmp_path / "missing.docx"
    cur.execute("INSERT INTO source_files (id, audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) VALUES (1, 1, 'Notes', 'notes.docx', ?, 'hash', '2023')", (str(notes_path),))
    
    registry = DummyRegistry(DummyEcoParser())
    conn.commit()
    with pytest.raises(BackfillSourceMissing):
        migrate_to_v14(conn, registry)

def test_v14_row_sequence_drift(tmp_path):
    conn = sqlite3.connect(":memory:")
    setup_v13_db(conn)
    cur = conn.cursor()
    cur.execute("INSERT INTO active_audits (id, part_number, work_order_ref, quantity, status, created_at, updated_at) VALUES (1, 'A', 'W', 1, 'Not Clear', '2023', '2023')")
    notes_path = tmp_path / "notes.docx"
    notes_path.touch()
    cur.execute("INSERT INTO source_files (id, audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) VALUES (1, 1, 'Notes', 'notes.docx', ?, 'hash', '2023')", (str(notes_path),))
    
    cur.execute("INSERT INTO build_notes_checklist (audit_id, source_file_id, row_sequence, original_text, is_verified) VALUES (1, 1, 1, 'A / B', 1)")
    
    res = EcoResult("A", [
        EcoItem(row_sequence=1, cells=("A", "B"), images=(), source_table_index=0),
        EcoItem(row_sequence=2, cells=("C", "D"), images=(), source_table_index=0)
    ], 1)
    registry = DummyRegistry(DummyEcoParser({notes_path: res}))
    conn.commit()
    with pytest.raises(MigrationError) as e:
        migrate_to_v14(conn, registry)
    assert "row_sequence drift" in str(e.value)

def test_v14_null_source_file_id(tmp_path):
    conn = sqlite3.connect(":memory:")
    setup_v13_db(conn)
    cur = conn.cursor()
    cur.execute("INSERT INTO active_audits (id, part_number, work_order_ref, quantity, status, created_at, updated_at) VALUES (1, 'A', 'W', 1, 'Not Clear', '2023', '2023')")
    
    cur.execute("INSERT INTO build_notes_checklist (audit_id, source_file_id, row_sequence, original_text, is_verified) VALUES (1, NULL, 1, 'A / B', 1)")
    
    registry = DummyRegistry(DummyEcoParser())
    conn.commit()
    with pytest.raises(MigrationError) as e:
        migrate_to_v14(conn, registry)
    assert "unattributable notes row" in str(e.value)
