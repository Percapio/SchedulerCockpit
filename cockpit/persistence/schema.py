"""Schema definitions and migrations."""

import sqlite3
from .clock import utcnow
from .errors import SchemaInitializationError, SchemaMismatch
from ..protocols import ParserRegistry


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
            if version >= 1:
                return  # v1 already applied; later migrations may have advanced the version
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


import json

SCHEMA_V2_DDL: str = """
ALTER TABLE active_audits ADD COLUMN ship_date TEXT NULL
"""

def migrate_to_v2(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=1)
        
    version = row["version"]
    if version >= 2:
        return  # v2 already applied; later migrations may have advanced the version
    if version < 1:
        raise SchemaMismatch(found_version=version, expected_version=1)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur.execute(SCHEMA_V2_DDL)
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement=SCHEMA_V2_DDL, cause=e)
            
        cur.execute("SELECT id, traveler_metadata FROM active_audits WHERE traveler_metadata IS NOT NULL")
        rows = cur.fetchall()
        for r in rows:
            # hydrating_row_factory already deserialised traveler_metadata to a dict
            payload = r["traveler_metadata"]
            if "lead_time_weeks" in payload:
                payload["lead_time_days"] = payload.pop("lead_time_weeks")
                cur.execute(
                    "UPDATE active_audits SET traveler_metadata = ? WHERE id = ?",
                    (json.dumps(payload), r["id"])
                )
                
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 2, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise


SCHEMA_V3_DDL_BOM_COMPONENTS: str = """
CREATE TABLE audit_bom_components (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id  INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    component_mpn   TEXT    NOT NULL,
    ref_des         TEXT    NOT NULL,
    mount_type      TEXT    NOT NULL CHECK (mount_type IN ('T','S')),
    description     TEXT    NULL,
    UNIQUE (source_file_id, ref_des)
)
"""

SCHEMA_V3_DDL_PDF_COORDS: str = """
CREATE TABLE pdf_component_coords (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id  INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    ref_des         TEXT    NOT NULL,
    page_index      INTEGER NOT NULL,
    x1              REAL    NOT NULL,
    y1              REAL    NOT NULL,
    x2              REAL    NOT NULL,
    y2              REAL    NOT NULL,
    UNIQUE (source_file_id, ref_des, page_index)
)
"""

SCHEMA_V3_DDL_SOURCE_FILES_REBUILD: str = """
CREATE TABLE source_files_v3 (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id           INTEGER NOT NULL REFERENCES active_audits(id) ON DELETE CASCADE,
    file_category      TEXT    NOT NULL CHECK (file_category IN ('BOM','Traveler','Notes','PDF')),
    original_filename  TEXT    NOT NULL,
    local_storage_path TEXT    NOT NULL,
    file_hash          TEXT    NOT NULL,
    ingested_at        TEXT    NOT NULL
)
"""

def migrate_to_v3(
    conn: sqlite3.Connection,
    parser_registry: ParserRegistry,
) -> None:
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=2)
        
    version = row["version"]
    if version >= 3:
        return
    if version < 2:
        raise SchemaMismatch(found_version=version, expected_version=2)
        
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur.execute(SCHEMA_V3_DDL_BOM_COMPONENTS)
            cur.execute(SCHEMA_V3_DDL_PDF_COORDS)
            cur.execute("CREATE INDEX ix_abc_source_file ON audit_bom_components(source_file_id)")
            cur.execute("CREATE INDEX ix_abc_mpn ON audit_bom_components(source_file_id, component_mpn)")
            cur.execute("CREATE INDEX ix_pcc_source_file_page ON pdf_component_coords(source_file_id, page_index)")
            
            cur.execute(SCHEMA_V3_DDL_SOURCE_FILES_REBUILD)
            cur.execute("INSERT INTO source_files_v3 SELECT * FROM source_files")
            cur.execute("DROP TABLE source_files")
            cur.execute("ALTER TABLE source_files_v3 RENAME TO source_files")
            cur.execute("CREATE INDEX ix_source_files_audit ON source_files(audit_id)")
            cur.execute("CREATE INDEX ix_source_files_hash ON source_files(file_hash)")
            
            cur.execute("PRAGMA foreign_key_check")
            fk_violations = cur.fetchall()
            if fk_violations:
                raise sqlite3.IntegrityError(f"Foreign key check failed: {fk_violations}")
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement="v3 DDL/rebuild", cause=e)
            
        cur.execute("SELECT DISTINCT id, local_storage_path FROM source_files WHERE file_category = 'BOM'")
        bom_files = cur.fetchall()
        import pathlib
        from ..ingestion.errors import MalformedBomError
        
        for sf in bom_files:
            sf_id = sf["id"]
            path = pathlib.Path(sf["local_storage_path"])
            if not path.exists():
                raise MalformedBomError(path, "BACKFILL_FILE_MISSING", {"source_file_id": sf_id, "local_storage_path": str(path)})
            
            bom_result = parser_registry.bom_parser.parse(path)
            for item in bom_result.items:
                if item.ref_des_list:
                    for rd in item.ref_des_list:
                        cur.execute(
                            """
                            INSERT INTO audit_bom_components 
                            (source_file_id, component_mpn, ref_des, mount_type, description)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (sf_id, item.component_mpn, rd, item.mount_type, item.description)
                        )
                        
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 3, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        cur.execute("PRAGMA foreign_keys = ON")


SCHEMA_V4_DDL_ACTIVE_AUDITS: str = """
ALTER TABLE active_audits ADD COLUMN general_notes TEXT NULL
"""

SCHEMA_V4_DDL_DROP_THT_NOTES: str = """
ALTER TABLE tht_verification_checklist DROP COLUMN notes
"""

SCHEMA_V4_DDL_DROP_BUILD_NOTES: str = """
ALTER TABLE build_notes_checklist DROP COLUMN notes
"""

def migrate_to_v4(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=3)
        
    version = row["version"]
    if version >= 4:
        return
    if version < 3:
        raise SchemaMismatch(found_version=version, expected_version=3)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur.execute(SCHEMA_V4_DDL_ACTIVE_AUDITS)
            cur.execute(SCHEMA_V4_DDL_DROP_THT_NOTES)
            cur.execute(SCHEMA_V4_DDL_DROP_BUILD_NOTES)
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement="v4 DDL", cause=e)
            
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 4, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

def migrate(conn: sqlite3.Connection, parser_registry: ParserRegistry) -> None:
    migrate_to_v1(conn)
    migrate_to_v2(conn)
    migrate_to_v3(conn, parser_registry)
    migrate_to_v4(conn)
