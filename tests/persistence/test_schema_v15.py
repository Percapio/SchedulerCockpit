import sqlite3
import pytest
from cockpit.persistence.schema import migrate_to_v15
from cockpit.persistence.errors import SchemaMismatch

def setup_v14_db(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("CREATE TABLE schema_version (singleton_guard INTEGER NOT NULL PRIMARY KEY CHECK (singleton_guard = 1), version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    cur.execute("INSERT INTO schema_version (singleton_guard, version, applied_at) VALUES (1, 14, '2023-01-01T00:00:00Z')")
    
    cur.execute("CREATE TABLE active_audits (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE source_files (id INTEGER PRIMARY KEY)")
    
    # Create the tables exactly as they look in v14 (with is_verified)
    cur.execute("CREATE TABLE build_notes_checklist (id INTEGER PRIMARY KEY, audit_id INTEGER NOT NULL, source_file_id INTEGER, row_sequence INTEGER NOT NULL, original_text TEXT NOT NULL, is_verified BOOLEAN NOT NULL DEFAULT 0)")
    cur.execute("CREATE TABLE tht_verification_checklist (id INTEGER PRIMARY KEY, audit_id INTEGER NOT NULL, source_file_id INTEGER, component_mpn TEXT NOT NULL, description TEXT, is_verified BOOLEAN NOT NULL DEFAULT 0)")
    
    conn.commit()

def test_migrate_to_v15(tmp_path):
    conn = sqlite3.connect(":memory:")
    setup_v14_db(conn)
    
    cur = conn.cursor()
    cur.execute("INSERT INTO build_notes_checklist (audit_id, source_file_id, row_sequence, original_text, is_verified) VALUES (1, 1, 1, 'text', 1)")
    cur.execute("INSERT INTO tht_verification_checklist (audit_id, source_file_id, component_mpn, description, is_verified) VALUES (1, 1, 'MPN', 'desc', 1)")
    conn.commit()
    
    assert migrate_to_v15(conn) is True
    
    # Verify is_verified column is dropped
    cur.execute("PRAGMA table_info(build_notes_checklist)")
    columns = [row["name"] for row in cur.fetchall()]
    assert "is_verified" not in columns
    assert "original_text" in columns
    
    cur.execute("PRAGMA table_info(tht_verification_checklist)")
    columns = [row["name"] for row in cur.fetchall()]
    assert "is_verified" not in columns
    assert "component_mpn" in columns
    
    # Idempotency
    assert migrate_to_v15(conn) is False

def test_v15_schema_mismatch():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE schema_version (singleton_guard INTEGER NOT NULL PRIMARY KEY CHECK (singleton_guard = 1), version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    cur.execute("INSERT INTO schema_version (singleton_guard, version, applied_at) VALUES (1, 13, '2023-01-01T00:00:00Z')")
    conn.commit()
    
    with pytest.raises(SchemaMismatch):
        migrate_to_v15(conn)
