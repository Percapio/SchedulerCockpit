"""Patch 08 §6 -- v16 drops the build-notes storage tables."""

import sqlite3

import pytest

from cockpit.persistence.errors import SchemaMismatch
from cockpit.persistence.schema import migrate_to_v16


def setup_v15_db(conn: sqlite3.Connection, version: int = 15) -> None:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE schema_version ("
        "singleton_guard INTEGER NOT NULL PRIMARY KEY CHECK (singleton_guard = 1), "
        "version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    cur.execute(
        "INSERT INTO schema_version (singleton_guard, version, applied_at) "
        "VALUES (1, ?, '2023-01-01T00:00:00Z')",
        (version,),
    )
    cur.execute(
        "CREATE TABLE build_notes_checklist ("
        "id INTEGER PRIMARY KEY, audit_id INTEGER NOT NULL, source_file_id INTEGER, "
        "row_sequence INTEGER NOT NULL, cells TEXT NOT NULL DEFAULT '[]', "
        "image_refs TEXT NOT NULL DEFAULT '[]', source_table_index INTEGER NOT NULL DEFAULT 0)"
    )
    cur.execute("CREATE INDEX ix_notes_audit_seq ON build_notes_checklist(audit_id, row_sequence)")
    cur.execute(
        "CREATE TABLE notes_media_cache ("
        "notes_file_hash TEXT NOT NULL, blob_sha1 TEXT NOT NULL, "
        "last_used_at TEXT NOT NULL, PRIMARY KEY (notes_file_hash, blob_sha1))"
    )
    cur.execute("CREATE INDEX ix_notes_media_lru ON notes_media_cache(last_used_at)")
    conn.commit()


def table_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in cur.fetchall()}


def index_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    return {row["name"] for row in cur.fetchall()}


def version_of(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    return cur.fetchone()["version"]


def test_forward_migration_from_v15_drops_both_tables_and_both_indexes():
    conn = sqlite3.connect(":memory:")
    setup_v15_db(conn)
    conn.cursor().execute(
        "INSERT INTO build_notes_checklist (audit_id, row_sequence) VALUES (1, 1)"
    )
    conn.commit()

    assert migrate_to_v16(conn) is True

    tables = table_names(conn)
    assert "build_notes_checklist" not in tables
    assert "notes_media_cache" not in tables

    indexes = index_names(conn)
    assert "ix_notes_audit_seq" not in indexes
    assert "ix_notes_media_lru" not in indexes

    assert version_of(conn) == 16


def test_rerun_at_v16_returns_false_without_touching_the_database():
    conn = sqlite3.connect(":memory:")
    setup_v15_db(conn)
    assert migrate_to_v16(conn) is True
    assert migrate_to_v16(conn) is False
    assert version_of(conn) == 16


def test_below_v15_raises_schema_mismatch():
    conn = sqlite3.connect(":memory:")
    setup_v15_db(conn, version=14)
    with pytest.raises(SchemaMismatch):
        migrate_to_v16(conn)
    assert "build_notes_checklist" in table_names(conn)


def test_a_database_that_never_reached_v14_still_migrates():
    """DROP TABLE IF EXISTS covers a chain that never created notes_media_cache."""
    conn = sqlite3.connect(":memory:")
    setup_v15_db(conn)
    cur = conn.cursor()
    cur.execute("DROP INDEX ix_notes_media_lru")
    cur.execute("DROP TABLE notes_media_cache")
    conn.commit()

    assert migrate_to_v16(conn) is True
    assert "build_notes_checklist" not in table_names(conn)


def test_no_schema_version_table_raises_schema_mismatch():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with pytest.raises(SchemaMismatch):
        migrate_to_v16(conn)
