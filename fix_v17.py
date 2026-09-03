def fix_v17_tests():
    with open('tests/persistence/test_schema_v17.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # Fix test_fresh_database_reaches_v17 and test_v17_is_idempotent
    content = content.replace('== 17', '== 18')

    rebuild = '''
    conn.execute("PRAGMA foreign_keys = OFF")
    # For backfill tests, we need to preserve the inserted data
    data = conn.execute("SELECT id, source_file_id, component_mpn, ref_des, mount_type, description FROM audit_bom_components").fetchall()
    
    conn.execute("DROP TABLE audit_bom_components")
    conn.execute("""
        CREATE TABLE audit_bom_components (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id  INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
            component_mpn   TEXT    NOT NULL,
            ref_des         TEXT    NOT NULL,
            mount_type      TEXT    NOT NULL CHECK (mount_type IN ('T','S')),
            description     TEXT    NULL,
            UNIQUE (source_file_id, ref_des)
        )
    """)
    for r in data:
        conn.execute(
            "INSERT INTO audit_bom_components (id, source_file_id, component_mpn, ref_des, mount_type, description) VALUES (?, ?, ?, ?, ?, ?)",
            (r["id"], r["source_file_id"], r["component_mpn"], r["ref_des"], r["mount_type"], r["description"])
        )
    conn.execute("PRAGMA foreign_keys = ON")
'''
    content = content.replace('    conn.execute("ALTER TABLE audit_bom_components DROP COLUMN find_number")', rebuild)

    with open('tests/persistence/test_schema_v17.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    fix_v17_tests()
