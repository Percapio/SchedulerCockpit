import re

def update_schema():
    with open('cockpit/persistence/schema.py', 'r', encoding='utf-8') as f:
        content = f.read()

    migrate_v18 = '''
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
    version = version_of(conn)
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
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as e:
            logger.error("Failed to restore foreign_keys in v18 migration finally block.")
            raise PersistenceError("Database state corrupted: failed to re-enable foreign keys") from e
'''
    content += '\n' + migrate_v18

    # Find the migrate function return statement and append v18
    # We want to insert `v18_migrated = migrate_to_v18(conn)` before `return (... or v17_migrated)`
    # And modify `return (... or v17_migrated)` to `return (... or v17_migrated or v18_migrated)`
    match = re.search(r'(v17_migrated = migrate_to_v17\(conn, parser_registry\)\n\s+)(return \(.*?\))', content, re.DOTALL)
    if match:
        v17_line = match.group(1)
        return_stmt = match.group(2)
        new_return_stmt = return_stmt.replace('v17_migrated)', 'v17_migrated or v18_migrated)')
        new_v18_line = 'v18_migrated = migrate_to_v18(conn)\n    '
        
        content = content[:match.start()] + v17_line + new_v18_line + new_return_stmt + content[match.end():]
        
        with open('cockpit/persistence/schema.py', 'w', encoding='utf-8') as f:
            f.write(content)
        print("Updated schema.py")
    else:
        print("Could not find the return statement in migrate()")

if __name__ == '__main__':
    update_schema()
