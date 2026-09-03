import pytest
import sqlite3
from cockpit.persistence.schema import migrate, migrate_to_v18
from cockpit.protocols import ParserRegistry
from cockpit.persistence.errors import SchemaInitializationError

class _NullBomParser:
    def parse(self, path):
        raise AssertionError("no BOM source file should reach the parser here")

@pytest.fixture
def null_registry():
    return ParserRegistry(_NullBomParser(), None, None, None, None)

def test_v18_migration_success(tmp_path, null_registry):
    db_path = tmp_path / "v18_success.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate(conn, null_registry)
    
    # We are already at v18, let's rollback to v17 for testing
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE audit_bom_components")
    conn.execute("""
        CREATE TABLE audit_bom_components (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id  INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
            component_mpn   TEXT    NOT NULL,
            ref_des         TEXT    NOT NULL,
            mount_type      TEXT    NOT NULL CHECK (mount_type IN ('T','S')),
            description     TEXT    NULL,
            find_number     INTEGER NOT NULL DEFAULT 0,
            UNIQUE (source_file_id, ref_des)
        )
    """)
    conn.execute("UPDATE schema_version SET version = 17 WHERE singleton_guard = 1")
    conn.execute("PRAGMA foreign_keys = ON")

    # Insert some audits/source files to satisfy foreign keys
    conn.execute(
        "INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (1, 'part', 'wo', '', 1, 'Not Clear', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source_files (id, audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (10, 1, 'BOM', 'file.xlsx', '/tmp/file.xlsx', 'hash', '2026-01-01T00:00:00')"
    )

    # Insert test rows
    conn.execute(
        "INSERT INTO audit_bom_components (id, source_file_id, component_mpn, ref_des, mount_type, find_number) "
        "VALUES (100, 10, 'MPN1', 'R1', 'S', 1), (101, 10, 'MPN2', 'R2', 'S', 2), (102, 10, 'MPN3', 'R3', 'S', 10)"
    )

    # Note sqlite_sequence
    cur = conn.cursor()
    cur.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit_bom_components'")
    pre_seq = cur.fetchone()["seq"]

    assert migrate_to_v18(conn) is True

    # Check version is 18
    assert conn.execute('SELECT version FROM schema_version WHERE singleton_guard = 1').fetchone()['version'] == 18

    # Row count, id values, sequence mark
    cur.execute("SELECT id, find_number FROM audit_bom_components ORDER BY id")
    rows = cur.fetchall()
    assert len(rows) == 3
    assert [(r["id"], r["find_number"]) for r in rows] == [(100, "1"), (101, "2"), (102, "10")]

    cur.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit_bom_components'")
    post_seq = cur.fetchone()["seq"]
    assert post_seq == pre_seq

    # Indices
    cur.execute("PRAGMA index_list('audit_bom_components')")
    indices = [r["name"] for r in cur.fetchall()]
    assert "ix_abc_source_file" in indices
    assert "ix_abc_mpn" in indices

    # Check new unique constraint: same ref_des on different find_numbers should insert successfully
    conn.execute(
        "INSERT INTO audit_bom_components (source_file_id, component_mpn, ref_des, mount_type, find_number) "
        "VALUES (10, 'MPN4', 'R1', 'S', '37A')"
    )
    # The same triple twice should fail
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO audit_bom_components (source_file_id, component_mpn, ref_des, mount_type, find_number) "
            "VALUES (10, 'MPN5', 'R1', 'S', '37A')"
        )

    # Idempotent
    assert migrate_to_v18(conn) is False

    # Check foreign keys
    cur.execute("PRAGMA foreign_keys")
    assert cur.fetchone()[0] == 1


def test_v18_rollback_on_failure(tmp_path, null_registry, monkeypatch):
    db_path = tmp_path / "v18_failure.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate(conn, null_registry)
    
    # Rollback to v17
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE audit_bom_components")
    conn.execute("""
        CREATE TABLE audit_bom_components (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id  INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
            component_mpn   TEXT    NOT NULL,
            ref_des         TEXT    NOT NULL,
            mount_type      TEXT    NOT NULL CHECK (mount_type IN ('T','S')),
            description     TEXT    NULL,
            find_number     INTEGER NOT NULL DEFAULT 0,
            UNIQUE (source_file_id, ref_des)
        )
    """)
    conn.execute("UPDATE schema_version SET version = 17 WHERE singleton_guard = 1")
    conn.execute("PRAGMA foreign_keys = ON")

    class MockCursor:
        def __init__(self, real_cur):
            self.real_cur = real_cur
        def execute(self, sql, *args):
            if "RENAME TO audit_bom_components" in sql:
                raise RuntimeError("Injected failure")
            return self.real_cur.execute(sql, *args)
        def fetchone(self): return self.real_cur.fetchone()
        def fetchall(self): return self.real_cur.fetchall()

    class ConnWrapper:
        def __init__(self, conn):
            self.conn = conn
        def cursor(self):
            return MockCursor(self.conn.cursor())
        def __getattr__(self, name):
            return getattr(self.conn, name)

    wrapped_conn = ConnWrapper(conn)
    with pytest.raises(SchemaInitializationError):
        migrate_to_v18(wrapped_conn)

    assert conn.execute('SELECT version FROM schema_version WHERE singleton_guard = 1').fetchone()['version'] == 17
    
    # Check original table intact (check if type is integer)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(audit_bom_components)")
    cols = {r["name"]: r["type"] for r in cur.fetchall()}
    assert cols["find_number"] == "INTEGER"

    cur.execute("PRAGMA foreign_keys")
    assert cur.fetchone()[0] == 1
