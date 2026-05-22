"""Schema definitions and migrations."""

import sqlite3
from .clock import utcnow
from .errors import SchemaInitializationError, SchemaMismatch


DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        singleton_guard INTEGER NOT NULL PRIMARY KEY CHECK (singleton_guard = 1),
        version         INTEGER NOT NULL,
        applied_at      TEXT    NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS active_audits (
        id INTEGER PRIMARY KEY,
        part_number VARCHAR NOT NULL,
        schedule_job_id INTEGER,
        work_order_ref VARCHAR NOT NULL,
        split_suffix VARCHAR NOT NULL DEFAULT '',
        quantity INTEGER NOT NULL,
        status VARCHAR NOT NULL CHECK(status IN ('Pending','InProgress','Completed')),
        split_reason TEXT,
        traveler_metadata TEXT,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        UNIQUE(part_number, work_order_ref, split_suffix)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS source_files (
        id INTEGER PRIMARY KEY,
        audit_id INTEGER NOT NULL,
        file_category VARCHAR NOT NULL CHECK(file_category IN ('BOM','Traveler','Notes')),
        original_filename VARCHAR NOT NULL,
        local_storage_path VARCHAR NOT NULL,
        file_hash VARCHAR NOT NULL,
        ingested_at DATETIME NOT NULL,
        FOREIGN KEY(audit_id) REFERENCES active_audits(id) ON DELETE CASCADE
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS tht_verification_checklist (
        id INTEGER PRIMARY KEY,
        audit_id INTEGER NOT NULL,
        source_file_id INTEGER,
        component_mpn VARCHAR NOT NULL,
        description TEXT,
        is_verified BOOLEAN NOT NULL DEFAULT 0,
        notes TEXT,
        FOREIGN KEY(audit_id) REFERENCES active_audits(id) ON DELETE CASCADE,
        FOREIGN KEY(source_file_id) REFERENCES source_files(id) ON DELETE CASCADE
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS build_notes_checklist (
        id INTEGER PRIMARY KEY,
        audit_id INTEGER NOT NULL,
        source_file_id INTEGER,
        row_sequence INTEGER NOT NULL,
        original_text TEXT NOT NULL,
        is_verified BOOLEAN NOT NULL DEFAULT 0,
        notes TEXT,
        FOREIGN KEY(audit_id) REFERENCES active_audits(id) ON DELETE CASCADE,
        FOREIGN KEY(source_file_id) REFERENCES source_files(id) ON DELETE CASCADE
    );
    """,
    """CREATE INDEX IF NOT EXISTS ix_source_files_audit ON source_files(audit_id);""",
    """CREATE INDEX IF NOT EXISTS ix_source_files_hash ON source_files(file_hash);""",
    """CREATE INDEX IF NOT EXISTS ix_tht_audit ON tht_verification_checklist(audit_id);""",
    """CREATE INDEX IF NOT EXISTS ix_notes_audit_seq ON build_notes_checklist(audit_id, row_sequence);"""
]


def migrate_to_v1(conn: sqlite3.Connection) -> None:
    """Initialize schema to v1 or verify existing schema is v1."""
    
    # First, check if schema_version exists. This query works safely
    # whether we are in a transaction or not.
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
        row = cur.fetchone()
        if row:
            version = row["version"]
            if version == 1:
                return  # Idempotent return
            if version > 1:
                raise SchemaMismatch(found_version=version, expected_version=1)
    except sqlite3.OperationalError:
        # Table does not exist -> fresh DB
        pass

    # No version row exists, so we migrate to v1.
    # Note: Using connection as context manager manages transactions (BEGIN ... COMMIT/ROLLBACK)
    # However, SQLite's isolation behavior can be finicky. We explicitly BEGIN IMMEDIATE.
    cur.execute("BEGIN IMMEDIATE")
    try:
        for stmt in DDL_STATEMENTS:
            try:
                cur.execute(stmt)
            except sqlite3.Error as e:
                raise SchemaInitializationError(statement=stmt, cause=e)

        # Insert the version row
        now_iso = utcnow().isoformat()
        try:
            cur.execute(
                "INSERT INTO schema_version (singleton_guard, version, applied_at) VALUES (1, 1, ?)",
                (now_iso,)
            )
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement="INSERT schema_version", cause=e)

        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
