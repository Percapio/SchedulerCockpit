import sqlite3
from pathlib import Path
from cockpit.services.library_errors import LibraryUnavailable

LIBRARY_SCHEMA_VERSION: int = 1

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        singleton_guard INTEGER NOT NULL PRIMARY KEY CHECK (singleton_guard = 1),
        version         INTEGER NOT NULL,
        applied_at      TEXT    NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS library_part (
        mpn_key                TEXT    PRIMARY KEY,
        mpn_as_seen            TEXT    NOT NULL,
        normalisation_version  INTEGER NOT NULL,
        manufacturer           TEXT    NULL,
        resolution_status      TEXT    NOT NULL
            CHECK (resolution_status IN
                   ('UNRESOLVED','RESOLVED','AMBIGUOUS','NOT_FOUND','UNKEYABLE')),
        selected_variant_ref   TEXT    NULL,
        first_seen_at          TEXT    NOT NULL,
        last_attempted_at      TEXT    NULL,
        last_resolved_at       TEXT    NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS part_attribute (
        mpn_key         TEXT    NOT NULL REFERENCES library_part(mpn_key)
                                         ON DELETE CASCADE,
        attribute_name  TEXT    NOT NULL,
        provenance      TEXT    NOT NULL
            CHECK (provenance IN ('OPERATOR_OVERRIDE','REFERENCE_SHEET','DIGIKEY')),
        value_text      TEXT    NOT NULL,
        value_numeric   REAL    NULL,
        source_label    TEXT    NULL,
        recorded_at     TEXT    NOT NULL,
        confirmed_at    TEXT    NULL,
        PRIMARY KEY (mpn_key, attribute_name, provenance)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS digikey_snapshot (
        mpn_key       TEXT    NOT NULL REFERENCES library_part(mpn_key)
                                       ON DELETE CASCADE,
        fetched_at    TEXT    NOT NULL,
        http_status   INTEGER NOT NULL,
        payload_json  TEXT    NOT NULL,
        payload_bytes INTEGER NOT NULL,
        PRIMARY KEY (mpn_key, fetched_at)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS library_quota_day (
        quota_date    TEXT    PRIMARY KEY,
        calls_made    INTEGER NOT NULL
    );
    """,
    """CREATE INDEX IF NOT EXISTS ix_part_attribute_name ON part_attribute(attribute_name);""",
    """CREATE INDEX IF NOT EXISTS ix_library_part_status ON library_part(resolution_status);"""
]

def migrate_library(db_path: Path) -> int:
    try:
        # Connect to the file. If it doesn't exist, sqlite3 creates it.
        # This will leave an empty file if migration fails later.
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        raise LibraryUnavailable(db_path, type(e))

    try:
        cur = conn.cursor()
        
        # Check current version
        try:
            cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
            row = cur.fetchone()
            current_version = row["version"] if row else 0
        except sqlite3.OperationalError:
            current_version = 0
            
        if current_version >= LIBRARY_SCHEMA_VERSION:
            return current_version
            
        # Run migration
        cur.execute("BEGIN IMMEDIATE")
        try:
            for stmt in DDL_STATEMENTS:
                cur.execute(stmt)
                
            from cockpit.persistence.clock import utcnow
            now_iso = utcnow().isoformat()
            
            # Upsert version row
            if current_version == 0:
                cur.execute(
                    "INSERT INTO schema_version (singleton_guard, version, applied_at) VALUES (1, ?, ?)",
                    (LIBRARY_SCHEMA_VERSION, now_iso)
                )
            else:
                cur.execute(
                    "UPDATE schema_version SET version = ?, applied_at = ? WHERE singleton_guard = 1",
                    (LIBRARY_SCHEMA_VERSION, now_iso)
                )
                
            cur.execute("COMMIT")
        except sqlite3.Error as e:
            cur.execute("ROLLBACK")
            raise LibraryUnavailable(db_path, type(e))
            
    finally:
        conn.close()
        
    return LIBRARY_SCHEMA_VERSION
