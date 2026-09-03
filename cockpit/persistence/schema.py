"""Schema definitions and migrations."""

import json
import pathlib
import sqlite3
import logging
from .clock import utcnow
from .errors import SchemaInitializationError, SchemaMismatch, BackfillSourceMissing, PersistenceError
from ..protocols import ParserRegistry
from ..ingestion.errors import MalformedBomError
from .traveler_flags import is_class_3, is_clean_process

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

SCHEMA_V4_DDL_DROP_THT_NOTES: str = """
ALTER TABLE tht_verification_checklist DROP COLUMN notes
"""

SCHEMA_V4_DDL_DROP_BUILD_NOTES: str = """
ALTER TABLE build_notes_checklist DROP COLUMN notes
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
        try:
            cur.execute(SCHEMA_V4_DDL_ACTIVE_AUDITS)
        except sqlite3.OperationalError:
            pass  # duplicate column name
            
        try:
            cur.execute(SCHEMA_V4_DDL_DROP_THT_NOTES)
        except sqlite3.OperationalError:
            pass  # no such column
            
        try:
            cur.execute(SCHEMA_V4_DDL_DROP_BUILD_NOTES)
        except sqlite3.OperationalError:
            pass  # no such column
            
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 4, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

SCHEMA_V7_DDL_ACTIVE_AUDITS_1: str = "ALTER TABLE active_audits ADD COLUMN ship_date TEXT NULL"
SCHEMA_V7_DDL_ACTIVE_AUDITS_2: str = "ALTER TABLE active_audits ADD COLUMN feeder_setuptime REAL NULL"
SCHEMA_V7_DDL_ACTIVE_AUDITS_3: str = "ALTER TABLE active_audits ADD COLUMN smt_runtime REAL NULL"
SCHEMA_V7_DDL_ACTIVE_AUDITS_4: str = "ALTER TABLE active_audits ADD COLUMN tht_runtime REAL NULL"
SCHEMA_V7_DDL_HOLIDAYS: str = "CREATE TABLE holidays ( holiday_date TEXT PRIMARY KEY )"

def migrate_to_v7(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=6)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=6)
        
    version = row["version"]
    if version >= 7:
        return False
    if version < 6:
        raise SchemaMismatch(found_version=version, expected_version=6)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        for ddl in [
            SCHEMA_V7_DDL_ACTIVE_AUDITS_1,
            SCHEMA_V7_DDL_ACTIVE_AUDITS_2,
            SCHEMA_V7_DDL_ACTIVE_AUDITS_3,
            SCHEMA_V7_DDL_ACTIVE_AUDITS_4,
            SCHEMA_V7_DDL_HOLIDAYS
        ]:
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError as e:
                # ignore duplicate column errors if we are retrying a partial migration
                if "duplicate column name" not in str(e) and "already exists" not in str(e):
                    raise SchemaInitializationError(statement="v7 DDL", cause=e)
                    
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 7, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        cur.execute("ROLLBACK")
        raise

def migrate_to_v8(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=7)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=7)
        
    version = row["version"]
    if version >= 8:
        return False
    if version < 7:
        raise SchemaMismatch(found_version=version, expected_version=7)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        # V8 migration: no DDL, just trigger backfill by bumping version.
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 8, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        cur.execute("ROLLBACK")
        raise

def migrate_to_v9(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=8)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=8)
        
    version = row["version"]
    if version >= 9:
        return False
    if version < 8:
        raise SchemaMismatch(found_version=version, expected_version=8)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        # V9 migration: no DDL, just trigger backfill by bumping version.
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 9, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        cur.execute("ROLLBACK")
        raise

SCHEMA_V10_DDL_AOI_RUNTIME: str = "ALTER TABLE active_audits ADD COLUMN aoi_runtime REAL NULL"
SCHEMA_V10_DDL_OPS_RUNTIME: str = "ALTER TABLE active_audits ADD COLUMN ops_runtime REAL NULL"
SCHEMA_V10_DDL_SHIPPING_RUNTIME: str = "ALTER TABLE active_audits ADD COLUMN shipping_runtime REAL NULL"
SCHEMA_V10_DDL_IS_CLASS_3: str = "ALTER TABLE active_audits ADD COLUMN is_class_3 INTEGER NOT NULL DEFAULT 0"
SCHEMA_V10_DDL_IS_CLEAN_PROCESS: str = "ALTER TABLE active_audits ADD COLUMN is_clean_process INTEGER NOT NULL DEFAULT 0"

def migrate_to_v10(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=9)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=9)
        
    current_version = row["version"]
    if current_version >= 10:
        return False
    if current_version < 9:
        raise SchemaMismatch(found_version=current_version, expected_version=9)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        for ddl in [
            SCHEMA_V10_DDL_AOI_RUNTIME,
            SCHEMA_V10_DDL_OPS_RUNTIME,
            SCHEMA_V10_DDL_SHIPPING_RUNTIME,
            SCHEMA_V10_DDL_IS_CLASS_3,
            SCHEMA_V10_DDL_IS_CLEAN_PROCESS,
        ]:
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError as add_column_error:
                if "duplicate column name" not in str(add_column_error):
                    raise SchemaInitializationError(statement="v10 DDL", cause=add_column_error)
                    
        cur.execute("SELECT id, traveler_metadata FROM active_audits")
        for audit_row in cur.fetchall():
            raw_metadata = audit_row["traveler_metadata"]
            if isinstance(raw_metadata, str):
                try:
                    traveler_metadata = json.loads(raw_metadata)
                except json.JSONDecodeError as parse_error:
                    raise SchemaInitializationError(statement="v10 traveler_metadata parse", cause=parse_error)
            else:
                traveler_metadata = raw_metadata
                
            cur.execute(
                "UPDATE active_audits SET is_class_3 = ?, is_clean_process = ? WHERE id = ?",
                (
                    1 if is_class_3(traveler_metadata) else 0,
                    1 if is_clean_process(traveler_metadata) else 0,
                    audit_row["id"],
                ),
            )
            
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 10, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return False
    except Exception:
        cur.execute("ROLLBACK")
        raise

SCHEMA_V11_DDL_OPS_PER_BOARD: str = "ALTER TABLE active_audits ADD COLUMN ops_per_board_min REAL NULL"

def migrate_to_v11(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=10)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=10)
        
    current_version = row["version"]
    if current_version >= 11:
        try:
            cur.execute(SCHEMA_V11_DDL_OPS_PER_BOARD)
        except sqlite3.OperationalError:
            pass
        return False
    if current_version < 10:
        raise SchemaMismatch(found_version=current_version, expected_version=10)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur.execute(SCHEMA_V11_DDL_OPS_PER_BOARD)
        except sqlite3.OperationalError as add_column_error:
            if "duplicate column name" not in str(add_column_error):
                raise SchemaInitializationError(statement="v11 DDL", cause=add_column_error)
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 11, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        conn.rollback()
        raise



def migrate(conn: sqlite3.Connection, parser_registry: ParserRegistry) -> bool:
    migrate_to_v1(conn)
    migrate_to_v2(conn)
    migrate_to_v3(conn, parser_registry)
    migrate_to_v4(conn)
    migrate_to_v5(conn)
    migrate_to_v6(conn)
    v7_migrated = migrate_to_v7(conn)
    v8_migrated = migrate_to_v8(conn)
    v9_migrated = migrate_to_v9(conn)
    migrate_to_v10(conn)
    v11_migrated = migrate_to_v11(conn)
    migrate_to_v12(conn)
    v13_migrated = migrate_to_v13(conn)
    v14_migrated = migrate_to_v14(conn, parser_registry)
    v15_migrated = migrate_to_v15(conn)
    v16_migrated = migrate_to_v16(conn)
    v17_migrated = migrate_to_v17(conn, parser_registry)
    v18_migrated = migrate_to_v18(conn)
    return (v7_migrated or v8_migrated or v9_migrated or v11_migrated or v13_migrated
            or v14_migrated or v15_migrated or v16_migrated or v17_migrated or v18_migrated)

SCHEMA_V5_DDL_DROP_SHIP_DATE: str = """
ALTER TABLE active_audits DROP COLUMN ship_date
"""

def migrate_to_v5(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=4)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=4)
        
    version = row["version"]
    if version >= 5:
        return
    if version < 4:
        raise SchemaMismatch(found_version=version, expected_version=4)
        
    cur.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur.execute(SCHEMA_V5_DDL_DROP_SHIP_DATE)
        except sqlite3.OperationalError:
            pass  # no such column
            
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 5, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise


SCHEMA_V6_DDL_NEW_TABLE: str = """
CREATE TABLE active_audits_v6 (
    id INTEGER PRIMARY KEY,
    part_number TEXT NOT NULL,
    schedule_job_id INTEGER,
    work_order_ref TEXT NOT NULL,
    split_suffix TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'Not Clear' CHECK(status IN ('Shipping','THT','SMT','Ready-to-Run','ON HOLD','Not Clear')),
    split_reason TEXT,
    traveler_metadata TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    general_notes TEXT NULL,
    UNIQUE(part_number, work_order_ref, split_suffix)
);
"""

SCHEMA_V6_DML_COPY: str = """
INSERT INTO active_audits_v6 (
    id, part_number, schedule_job_id, work_order_ref, split_suffix, quantity, 
    status, split_reason, traveler_metadata, created_at, updated_at, general_notes
)
SELECT 
    id, part_number, schedule_job_id, work_order_ref, split_suffix, quantity,
    CASE 
        WHEN status IN ('Pending', 'InProgress') THEN 'Not Clear'
        ELSE 'Not Clear' 
    END,
    split_reason, traveler_metadata, created_at, updated_at, general_notes
FROM active_audits;
"""

SCHEMA_V6_DDL_DROP: str = "DROP TABLE active_audits"
SCHEMA_V6_DDL_RENAME: str = "ALTER TABLE active_audits_v6 RENAME TO active_audits"

def migrate_to_v6(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=5)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=5)
        
    version = row["version"]
    if version >= 6:
        return
    if version < 5:
        raise SchemaMismatch(found_version=version, expected_version=5)
        
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute("SELECT COUNT(*) as cnt FROM active_audits WHERE status = 'Completed'")
        count = cur.fetchone()["cnt"]
        if count > 0:
            from .errors import MigrationError
            raise MigrationError(f"Found {count} 'Completed' audits. Cannot migrate.")

        try:
            cur.execute(SCHEMA_V6_DDL_NEW_TABLE)
            cur.execute(SCHEMA_V6_DML_COPY)
            cur.execute(SCHEMA_V6_DDL_DROP)
            cur.execute(SCHEMA_V6_DDL_RENAME)
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement="v6 migration", cause=e)
            
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 6, applied_at = ? WHERE singleton_guard = 1",
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
            logger.error("Failed to restore foreign_keys in v6 migration finally block.")
            from .errors import PersistenceError
            raise PersistenceError("Database state corrupted: failed to re-enable foreign keys")


STATUS_CHECK_V12: str = (
    "('Shipping','THT','SMT','FSU','AOI','OPS','ON HOLD','Not Clear')"
)

SCHEMA_V12_DDL_NEW_TABLE: str = f"""
CREATE TABLE active_audits_v12 (
    id INTEGER PRIMARY KEY,
    part_number TEXT NOT NULL,
    schedule_job_id INTEGER,
    work_order_ref TEXT NOT NULL,
    split_suffix TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'Not Clear' CHECK(status IN {STATUS_CHECK_V12}),
    split_reason TEXT,
    traveler_metadata TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    general_notes TEXT NULL,
    ship_date TEXT NULL,
    feeder_setuptime REAL NULL,
    smt_runtime REAL NULL,
    tht_runtime REAL NULL,
    aoi_runtime REAL NULL,
    ops_runtime REAL NULL,
    shipping_runtime REAL NULL,
    is_class_3 INTEGER NOT NULL DEFAULT 0,
    is_clean_process INTEGER NOT NULL DEFAULT 0,
    ops_per_board_min REAL NULL,
    is_labeled INTEGER NOT NULL DEFAULT 0,
    are_photos_uploaded INTEGER NOT NULL DEFAULT 0,
    UNIQUE(part_number, work_order_ref, split_suffix)
);
"""

SCHEMA_V12_DML_COPY: str = """
INSERT INTO active_audits_v12 (
    id, part_number, schedule_job_id, work_order_ref, split_suffix, quantity,
    status, split_reason, traveler_metadata, created_at, updated_at, general_notes,
    ship_date, feeder_setuptime, smt_runtime, tht_runtime, aoi_runtime, ops_runtime,
    shipping_runtime, is_class_3, is_clean_process, ops_per_board_min,
    is_labeled, are_photos_uploaded
)
SELECT
    id, part_number, schedule_job_id, work_order_ref, split_suffix, quantity,
    CASE WHEN status = 'Ready-to-Run' THEN 'Not Clear' ELSE status END,
    split_reason, traveler_metadata, created_at, updated_at, general_notes,
    ship_date, feeder_setuptime, smt_runtime, tht_runtime, aoi_runtime, ops_runtime,
    shipping_runtime, is_class_3, is_clean_process, ops_per_board_min,
    0, 0
FROM active_audits;
"""


def migrate_to_v12(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=11)

    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=11)

    current_version = row["version"]
    if current_version >= 12:
        return False
    if current_version < 11:
        raise SchemaMismatch(found_version=current_version, expected_version=11)

    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("BEGIN IMMEDIATE")
    try:
        for col_def in [
            "aoi_runtime REAL NULL",
            "ops_runtime REAL NULL",
            "shipping_runtime REAL NULL",
            "is_class_3 INTEGER NOT NULL DEFAULT 0",
            "is_clean_process INTEGER NOT NULL DEFAULT 0",
            "ops_per_board_min REAL NULL",
        ]:
            try:
                cur.execute(f"ALTER TABLE active_audits ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        try:
            cur.execute(SCHEMA_V12_DDL_NEW_TABLE)
            cur.execute(SCHEMA_V12_DML_COPY)
            cur.execute("DROP TABLE active_audits")
            cur.execute("ALTER TABLE active_audits_v12 RENAME TO active_audits")
            cur.execute("PRAGMA foreign_key_check")
            violations = cur.fetchall()
            if violations:
                raise SchemaInitializationError(
                    statement="v12 foreign_key_check",
                    cause=RuntimeError(f"Foreign key violations found: {violations}")
                )
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement="v12 migration", cause=e)

        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 12, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as e:
            logger.error("Failed to restore foreign_keys in v12 migration finally block.")
            raise PersistenceError("Database state corrupted: failed to re-enable foreign keys") from e


FILE_CATEGORY_CHECK_V13: str = "('BOM','Traveler','Notes','PDF','SecondaryPDF')"

SCHEMA_V13_DDL_NEW_TABLE: str = f"""
CREATE TABLE source_files_v13 (
    id INTEGER PRIMARY KEY,
    audit_id INTEGER NOT NULL,
    file_category TEXT NOT NULL CHECK(file_category IN {FILE_CATEGORY_CHECK_V13}),
    original_filename TEXT NOT NULL,
    local_storage_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    ingested_at DATETIME NOT NULL,
    FOREIGN KEY(audit_id) REFERENCES active_audits(id) ON DELETE CASCADE
);
"""

SCHEMA_V13_DML_COPY: str = """
INSERT INTO source_files_v13 SELECT * FROM source_files;
"""


def migrate_to_v13(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=12)

    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=12)

    current_version = row["version"]
    if current_version >= 13:
        return False
    if current_version < 12:
        raise SchemaMismatch(found_version=current_version, expected_version=12)

    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur.execute(SCHEMA_V13_DDL_NEW_TABLE)
            cur.execute(SCHEMA_V13_DML_COPY)
            cur.execute("DROP TABLE source_files")
            cur.execute("ALTER TABLE source_files_v13 RENAME TO source_files")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_source_files_audit ON source_files(audit_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_source_files_hash ON source_files(file_hash)")
            cur.execute("PRAGMA foreign_key_check")
            violations = cur.fetchall()
            if violations:
                raise SchemaInitializationError(
                    statement="v13 foreign_key_check",
                    cause=RuntimeError(f"Foreign key violations found: {violations}")
                )
        except sqlite3.Error as e:
            raise SchemaInitializationError(statement="v13 migration", cause=e)

        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 13, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as e:
            logger.error("Failed to restore foreign_keys in v13 migration finally block.")
            raise PersistenceError("Database state corrupted: failed to re-enable foreign keys") from e

SCHEMA_V14_DDL_NEW_TABLE: str = """
CREATE TABLE build_notes_checklist_v14 (
    id                 INTEGER PRIMARY KEY,
    audit_id           INTEGER NOT NULL,
    source_file_id     INTEGER,
    row_sequence       INTEGER NOT NULL,
    cells              TEXT    NOT NULL,              -- JSON array of strings
    image_refs         TEXT    NOT NULL DEFAULT '[]', -- JSON array of EcoImageRef
    source_table_index INTEGER NOT NULL DEFAULT 0,
    is_verified        BOOLEAN NOT NULL DEFAULT 0,
    FOREIGN KEY(audit_id) REFERENCES active_audits(id) ON DELETE CASCADE,
    FOREIGN KEY(source_file_id) REFERENCES source_files(id) ON DELETE CASCADE
);
"""

SCHEMA_V14_DDL_CACHE: str = """
CREATE TABLE notes_media_cache (
    notes_file_hash TEXT    PRIMARY KEY,  -- SourceFile.file_hash of the .docx
    extracted_at    TEXT    NOT NULL,
    last_used_at    TEXT    NOT NULL,
    byte_size       INTEGER NOT NULL,
    image_count     INTEGER NOT NULL
);
"""

SCHEMA_V14_DDL_CACHE_INDEX: str = """
CREATE INDEX ix_notes_media_lru ON notes_media_cache(last_used_at);
"""

def migrate_to_v14(conn: sqlite3.Connection, parser_registry: ParserRegistry) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=13)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=13)
        
    current_version = row["version"]
    if current_version >= 14:
        return False
    if current_version < 13:
        raise SchemaMismatch(found_version=current_version, expected_version=13)

    cur.execute("SELECT id, audit_id, local_storage_path FROM source_files WHERE file_category = 'Notes'")
    notes_files = cur.fetchall()
    
    staged_items = {}
    
    for sf in notes_files:
        path = pathlib.Path(sf["local_storage_path"])
        if not path.exists():
            raise BackfillSourceMissing(statement="v14 notes backfill", cause=Exception(f"Missing notes file {path} for audit {sf['audit_id']}"))
            
        try:
            eco_result = parser_registry.eco_parser.parse(path)
        except Exception as e:
            raise SchemaInitializationError(statement="v14 notes backfill", cause=e)
            
        cur.execute("SELECT COUNT(*) as c FROM build_notes_checklist WHERE audit_id = ? AND source_file_id = ?", (sf["audit_id"], sf["id"]))
        db_count = cur.fetchone()["c"]
        
        if len(eco_result.items) != db_count:
            from .errors import MigrationError
            raise MigrationError(f"row_sequence drift: audit {sf['audit_id']} has {db_count} rows but parsed {len(eco_result.items)}")
            
        staged_items[(sf["audit_id"], sf["id"])] = eco_result.items
        
    cur.execute("SELECT id FROM build_notes_checklist WHERE source_file_id IS NULL")
    if cur.fetchone():
        from .errors import MigrationError
        raise MigrationError("unattributable notes row found with source_file_id IS NULL")
        
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute(SCHEMA_V14_DDL_NEW_TABLE)
        cur.execute(SCHEMA_V14_DDL_CACHE)
        cur.execute(SCHEMA_V14_DDL_CACHE_INDEX)
        
        cur.execute("SELECT audit_id, source_file_id, row_sequence, is_verified FROM build_notes_checklist")
        verified_state = {}
        for r in cur.fetchall():
            verified_state[(r["audit_id"], r["source_file_id"], r["row_sequence"])] = r["is_verified"]
            
        for (audit_id, sf_id), items in staged_items.items():
            for item in items:
                is_verified = verified_state.get((audit_id, sf_id, item.row_sequence), 0)
                images_json = json.dumps([
                    {"blob_sha1": img.blob_sha1, "content_type": img.content_type, "cell_index": img.cell_index, "order": img.order}
                    for img in item.images
                ])
                cells_json = json.dumps(item.cells)
                cur.execute(
                    """
                    INSERT INTO build_notes_checklist_v14 (
                        audit_id, source_file_id, row_sequence, cells, image_refs, source_table_index, is_verified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (audit_id, sf_id, item.row_sequence, cells_json, images_json, item.source_table_index, is_verified)
                )
                
        cur.execute("DROP TABLE build_notes_checklist")
        cur.execute("ALTER TABLE build_notes_checklist_v14 RENAME TO build_notes_checklist")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_notes_audit_seq ON build_notes_checklist(audit_id, row_sequence)")
        
        cur.execute("PRAGMA foreign_key_check")
        violations = cur.fetchall()
        if violations:
            raise SchemaInitializationError(
                statement="v14 foreign_key_check",
                cause=RuntimeError(f"Foreign key violations found: {violations}")
            )
            
        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 14, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as e:
            logger.error("Failed to restore foreign_keys in v14 migration finally block.")
            raise PersistenceError("Database state corrupted: failed to re-enable foreign keys") from e


def migrate_to_v15(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=14)
        
    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=14)
        
    current_version = row[0] if isinstance(row, tuple) and not hasattr(row, 'keys') else row["version"]
    if current_version >= 15:
        return False
    if current_version < 14:
        raise SchemaMismatch(found_version=current_version, expected_version=14)

    cur.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur.execute("ALTER TABLE build_notes_checklist DROP COLUMN is_verified")
        except sqlite3.OperationalError:
            pass
        
        try:
            cur.execute("ALTER TABLE tht_verification_checklist DROP COLUMN is_verified")
        except sqlite3.OperationalError:
            pass

        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 15, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        conn.rollback()
        raise


def migrate_to_v16(conn: sqlite3.Connection) -> bool:
    """Removes the build-notes storage tables. Content is re-derived from the .docx.

    pre:  schema_version >= 15
    post: neither table exists; schema_version >= 16. Returns False without
          touching the database when already >= 16.
    raises: SchemaMismatch when version < 15
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=15)

    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=15)

    current_version = row[0] if isinstance(row, tuple) and not hasattr(row, 'keys') else row["version"]
    if current_version >= 16:
        return False
    if current_version < 15:
        raise SchemaMismatch(found_version=current_version, expected_version=15)

    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute("DROP INDEX IF EXISTS ix_notes_audit_seq")
        cur.execute("DROP INDEX IF EXISTS ix_notes_media_lru")
        cur.execute("DROP TABLE IF EXISTS build_notes_checklist")
        cur.execute("DROP TABLE IF EXISTS notes_media_cache")

        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 16, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        conn.rollback()
        raise


def migrate_to_v17(conn: sqlite3.Connection, parser_registry: ParserRegistry) -> bool:
    """Repairs audit_bom_components.find_number on databases created before it existed.

    The column was added to the v3 CREATE TABLE rather than as its own
    migration, so a database that had already passed v3 never received it.
    Every BOM read path selects the column, so such a database fails on any BOM
    query -- including the 2nd OPS candidate scan.

    pre:  schema_version >= 16
    post: audit_bom_components has find_number; rows are backfilled from the
          stored Audit BOM wherever it can still be parsed and left at 0 where
          it cannot. schema_version >= 17.
    raises: SchemaMismatch when version < 16
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    except sqlite3.OperationalError:
        raise SchemaMismatch(found_version=0, expected_version=16)

    row = cur.fetchone()
    if not row:
        raise SchemaMismatch(found_version=0, expected_version=16)

    current_version = row[0] if isinstance(row, tuple) and not hasattr(row, 'keys') else row["version"]
    if current_version >= 17:
        return False
    if current_version < 16:
        raise SchemaMismatch(found_version=current_version, expected_version=16)

    cur.execute("PRAGMA table_info(audit_bom_components)")
    has_find_number = any(
        (r[1] if isinstance(r, tuple) and not hasattr(r, 'keys') else r["name"]) == "find_number"
        for r in cur.fetchall()
    )

    cur.execute("BEGIN IMMEDIATE")
    try:
        if not has_find_number:
            cur.execute(
                "ALTER TABLE audit_bom_components "
                "ADD COLUMN find_number INTEGER NOT NULL DEFAULT 0"
            )
            _backfill_find_numbers(cur, parser_registry)

        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 17, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception:
        conn.rollback()
        raise


def _backfill_find_numbers(cur: sqlite3.Cursor, parser_registry: ParserRegistry) -> None:
    """Best-effort find_number recovery from the stored Audit BOM workbooks.

    Unlike the v3 backfill this never aborts the migration: an upload that has
    since been deleted or edited leaves those rows at 0, which degrades
    ordering but keeps every BOM read path working. Refusing to start because
    one workbook is unreadable is the worse failure.
    """
    cur.execute(
        "SELECT DISTINCT id, local_storage_path FROM source_files WHERE file_category = 'BOM'"
    )
    bom_files = cur.fetchall()

    for sf in bom_files:
        is_tuple = isinstance(sf, tuple) and not hasattr(sf, 'keys')
        sf_id = sf[0] if is_tuple else sf["id"]
        raw_path = sf[1] if is_tuple else sf["local_storage_path"]
        path = pathlib.Path(raw_path)
        if not path.exists():
            logger.warning("v17 backfill: BOM file missing for source_file %s at %s", sf_id, path)
            continue

        try:
            bom_result = parser_registry.bom_parser.parse(path)
        except Exception as exc:
            logger.warning("v17 backfill: could not parse BOM for source_file %s: %s", sf_id, exc)
            continue

        for item in bom_result.items:
            cur.execute(
                "UPDATE audit_bom_components SET find_number = ? "
                "WHERE source_file_id = ? AND component_mpn = ?",
                (item.find_number, sf_id, item.component_mpn)
            )


def migrate_to_v18(conn: sqlite3.Connection) -> bool:
    """
    Converts audit_bom_components.find_number to TEXT and widens the uniqueness
    key so one Ref_Des may appear on more than one BOM line.
    pre:  schema_version >= 17; no ingestion in flight
    post: find_number has TEXT affinity, every prior value preserved as its
          decimal text form; UNIQUE is (source_file_id, find_number, ref_des);
          ix_abc_source_file and ix_abc_mpn exist; schema_version == 18.
          On any failure the table is untouched and the version unchanged.
    raises: SchemaMismatch when version < 17;
            SchemaInitializationError on DDL failure or FK violation;
            PersistenceError when foreign_keys cannot be restored
    """
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    version = cur.fetchone()["version"]
    if version < 17:
        raise SchemaMismatch(f"Cannot run v18 migration from version {version}")
    if version >= 18:
        return False

    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute("""
        CREATE TABLE audit_bom_components_v18 (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id  INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
            component_mpn   TEXT    NOT NULL,
            ref_des         TEXT    NOT NULL,
            mount_type      TEXT    NOT NULL CHECK (mount_type IN ('T','S')),
            description     TEXT    NULL,
            find_number     TEXT    NOT NULL,
            UNIQUE (source_file_id, find_number, ref_des)
        )
        """)

        cur.execute("""
        INSERT INTO audit_bom_components_v18
            (id, source_file_id, component_mpn, ref_des, mount_type, description, find_number)
        SELECT
             id, source_file_id, component_mpn, ref_des, mount_type, description,
             CAST(find_number AS TEXT)
        FROM audit_bom_components;
        """)

        cur.execute("DROP TABLE audit_bom_components")
        cur.execute("ALTER TABLE audit_bom_components_v18 RENAME TO audit_bom_components")
        cur.execute("CREATE INDEX ix_abc_source_file ON audit_bom_components(source_file_id)")
        cur.execute("CREATE INDEX ix_abc_mpn         ON audit_bom_components(source_file_id, component_mpn)")

        cur.execute("PRAGMA foreign_key_check")
        violations = cur.fetchall()
        if violations:
            raise SchemaInitializationError(
                statement="v18 foreign_key_check",
                cause=RuntimeError(f"Foreign key violations found: {violations}")
            )

        now_iso = utcnow().isoformat()
        cur.execute(
            "UPDATE schema_version SET version = 18, applied_at = ? WHERE singleton_guard = 1",
            (now_iso,)
        )
        cur.execute("COMMIT")
        return True
    except Exception as e:
        conn.rollback()
        raise SchemaInitializationError(statement="v18 migration failed", cause=e)
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as e:
            logger.error("Failed to restore foreign_keys in v18 migration finally block.")
            raise PersistenceError("Database state corrupted: failed to re-enable foreign keys") from e
