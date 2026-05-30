"""Schema definitions and migrations."""

import json
import pathlib
import sqlite3
import logging
from .clock import utcnow
from .errors import SchemaInitializationError, SchemaMismatch, BackfillSourceMissing, PersistenceError
from ..protocols import ParserRegistry
from ..ingestion.errors import MalformedBomError

logger = logging.getLogger(__name__)

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
        part_number TEXT NOT NULL,
        schedule_job_id INTEGER,
        work_order_ref TEXT NOT NULL,
        split_suffix TEXT NOT NULL DEFAULT '',
        quantity INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('Pending','InProgress','Completed')),
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
        file_category TEXT NOT NULL CHECK(file_category IN ('BOM','Traveler','Notes','PDF')),
        original_filename TEXT NOT NULL,
        local_storage_path TEXT NOT NULL,
        file_hash TEXT NOT NULL,
        ingested_at DATETIME NOT NULL,
        FOREIGN KEY(audit_id) REFERENCES active_audits(id) ON DELETE CASCADE
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS tht_verification_checklist (
        id INTEGER PRIMARY KEY,
        audit_id INTEGER NOT NULL,
        source_file_id INTEGER,
        component_mpn TEXT NOT NULL,
        description TEXT,
        is_verified BOOLEAN NOT NULL DEFAULT 0,
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
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
        row = cur.fetchone()
        if row and row["version"] >= 1:
            return
    except sqlite3.OperationalError:
        pass

    cur.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
            row = cur.fetchone()
            if row and row["version"] >= 1:
                cur.execute("COMMIT")
                return
        except sqlite3.OperationalError:
            pass

        for stmt in DDL_STATEMENTS:
            try:
                cur.execute(stmt)
            except sqlite3.Error as e:
                raise SchemaInitializationError(statement=stmt, cause=e)

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


SCHEMA_V2_DDL: str = """
ALTER TABLE active_audits ADD COLUMN ship_date TEXT NULL
"""

def migrate_to_v2(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=1)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=1)
        
    version = row["version"]
    if version >= 2:
        return
    if version < 1:
        raise SchemaMismatch(found_version=version, expected_version=1)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
        row = cur.fetchone()
        if row["version"] >= 2:
            cur.execute("COMMIT")
            return

        try:
            cur.execute(SCHEMA_V2_DDL)
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement=SCHEMA_V2_DDL, cause=e)
            
        cur.execute("SELECT id, traveler_metadata FROM active_audits WHERE traveler_metadata IS NOT NULL")
        rows = cur.fetchall()
        for r in rows:
            try:
                payload = r["traveler_metadata"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if "lead_time_weeks" in payload:
                    payload["lead_time_days"] = payload.pop("lead_time_weeks")
                    cur.execute(
                        "UPDATE active_audits SET traveler_metadata = ? WHERE id = ?",
                        (json.dumps(payload), r["id"])
                    )
            except json.JSONDecodeError as e:
                raise SchemaInitializationError(statement="v2 traveler_metadata parse", cause=e)
                
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
    find_number     INTEGER NOT NULL,
    UNIQUE (source_file_id, ref_des)
)
-- Note: uniqueness on (source_file_id, component_mpn) is NOT enforced by DB.
-- find_number 1:1 with MPN relies entirely on audit_bom.parse DUPLICATE_MPN check.
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

def migrate_to_v3(
    conn: sqlite3.Connection,
    parser_registry: ParserRegistry,
) -> None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=2)
        
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
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
        row = cur.fetchone()
        if row["version"] >= 3:
            cur.execute("COMMIT")
            return

        try:
            cur.execute(SCHEMA_V3_DDL_BOM_COMPONENTS)
            cur.execute(SCHEMA_V3_DDL_PDF_COORDS)
            cur.execute("CREATE INDEX ix_abc_source_file ON audit_bom_components(source_file_id)")
            cur.execute("CREATE INDEX ix_abc_mpn ON audit_bom_components(source_file_id, component_mpn)")
            cur.execute("CREATE INDEX ix_pcc_source_file_page ON pdf_component_coords(source_file_id, page_index)")
            
            cur.execute("PRAGMA foreign_key_check")
            fk_violations = cur.fetchall()
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement="v3 DDL", cause=e)

        if fk_violations:
            raise sqlite3.IntegrityError(f"Foreign key check failed: {fk_violations}")
            
        cur.execute("SELECT DISTINCT id, local_storage_path FROM source_files WHERE file_category = 'BOM'")
        bom_files = cur.fetchall()
        
        empty_ref_des_count = 0

        for sf in bom_files:
            sf_id = sf["id"]
            path = pathlib.Path(sf["local_storage_path"])
            if not path.exists():
                raise BackfillSourceMissing(statement="v3 BOM backfill", cause=Exception(f"Missing BOM file {path}"))
            
            try:
                bom_result = parser_registry.bom_parser.parse(path)
            except MalformedBomError as e:
                raise SchemaInitializationError(statement="v3 BOM backfill", cause=e)

            for item in bom_result.items:
                if item.ref_des_list:
                    for rd in item.ref_des_list:
                        cur.execute(
                            """
                            INSERT INTO audit_bom_components 
                            (source_file_id, component_mpn, ref_des, mount_type, description, find_number)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (sf_id, item.component_mpn, rd, item.mount_type, item.description, item.find_number)
                        )
                else:
                    empty_ref_des_count += 1
                    
        if empty_ref_des_count > 0:
            logger.info(f"Skipped {empty_ref_des_count} BOM items with empty ref_des_list during v3 backfill")
                        
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
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            logger.error("Failed to restore foreign_keys in v3 migration finally block.")
            raise PersistenceError("Database state corrupted: failed to re-enable foreign keys")


SCHEMA_V4_DDL_ACTIVE_AUDITS: str = """
ALTER TABLE active_audits ADD COLUMN general_notes TEXT NULL
"""

def migrate_to_v4(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=3)
        
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
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
        row = cur.fetchone()
        if row["version"] >= 4:
            cur.execute("COMMIT")
            return

        try:
            cur.execute(SCHEMA_V4_DDL_ACTIVE_AUDITS)
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
