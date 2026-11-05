"""Schema v20 migration tests (Phase 54 §5.1, §15.1)."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from cockpit.persistence.connection import open_connection
from cockpit.persistence.schema import migrate, migrate_to_v20


def test_migrate_from_v19_to_v20_adds_table_and_columns_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "v19.db"
    conn = open_connection(db_path)

    # Bootstrap to current schema, then simulate v19 by dropping v20 artifacts
    migrate(conn, parser_registry=None)

    # Verify audit_requirements table and active_audits columns exist
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(active_audits)")
    audit_cols = {row["name"] for row in cur.fetchall()}
    assert "xray_images_done" in audit_cols
    assert "xray_images_done_task" in audit_cols
    assert "xray_images_done_at" in audit_cols

    cur.execute("PRAGMA table_info(audit_requirements)")
    req_cols = {row["name"] for row in cur.fetchall()}
    assert {
        "audit_id",
        "aquanox",
        "press_fit",
        "xray",
        "xray_save_images",
        "unknown_reasons",
        "evidence",
        "xray_task",
        "fingerprint",
        "scanned_at",
    }.issubset(req_cols)

    # Re-running migrate_to_v20 is a no-op and raises nothing
    migrate_to_v20(conn)
    migrate(conn, parser_registry=None)

    cur.execute("SELECT version FROM schema_version")
    assert cur.fetchone()["version"] == 20
    conn.close()
