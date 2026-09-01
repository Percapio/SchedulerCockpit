"""v17 repairs audit_bom_components.find_number.

The column was added to the v3 CREATE TABLE rather than as its own migration,
so a database that had already passed v3 never received it. Every BOM read
path selects the column, so those databases fail on any BOM query.
"""

import sqlite3

import pytest

from cockpit.persistence.connection import hydrating_row_factory
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.schema import migrate, migrate_to_v17
from cockpit.protocols import ParserRegistry


class _NullBomParser:
    def parse(self, path):
        raise AssertionError("no BOM source file should reach the parser here")


@pytest.fixture
def null_registry():
    return ParserRegistry(_NullBomParser(), None, None, None, None)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _open(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory
    return conn


def test_fresh_database_reaches_v17(tmp_path, null_registry):
    conn = _open(tmp_path / "fresh.db")
    migrate(conn, null_registry)

    assert conn.execute(
        "SELECT version FROM schema_version WHERE singleton_guard = 1"
    ).fetchone()["version"] == 17
    assert "find_number" in _columns(conn, "audit_bom_components")


def test_v17_adds_the_missing_column(tmp_path, null_registry):
    """A v16 database whose table predates find_number gains it."""
    db_path = tmp_path / "legacy.db"
    conn = _open(db_path)
    migrate(conn, null_registry)

    # Reproduce the legacy shape: drop the column and wind the version back.
    conn.execute("ALTER TABLE audit_bom_components DROP COLUMN find_number")
    conn.execute("UPDATE schema_version SET version = 16 WHERE singleton_guard = 1")
    assert "find_number" not in _columns(conn, "audit_bom_components")

    assert migrate_to_v17(conn, null_registry) is True

    assert "find_number" in _columns(conn, "audit_bom_components")
    assert conn.execute(
        "SELECT version FROM schema_version WHERE singleton_guard = 1"
    ).fetchone()["version"] == 17


def test_v17_is_idempotent(tmp_path, null_registry):
    conn = _open(tmp_path / "twice.db")
    migrate(conn, null_registry)

    assert migrate_to_v17(conn, null_registry) is False
    assert conn.execute(
        "SELECT version FROM schema_version WHERE singleton_guard = 1"
    ).fetchone()["version"] == 17


def test_bom_reads_work_after_the_repair(tmp_path, null_registry):
    """The symptom v17 exists to remove: every BOM select names find_number."""
    conn = _open(tmp_path / "repaired.db")
    migrate(conn, null_registry)

    conn.execute("ALTER TABLE audit_bom_components DROP COLUMN find_number")
    conn.execute("UPDATE schema_version SET version = 16 WHERE singleton_guard = 1")

    repo = AuditBomComponentRepository(conn)
    with pytest.raises(Exception):
        repo.list_for_source_file(1)

    migrate_to_v17(conn, null_registry)

    assert repo.list_for_source_file(1) == []
    assert repo.list_bom_lines_for_all_active_audits() == []


def test_v17_backfills_from_the_stored_workbook(tmp_path):
    """Rows recover their find_number where the workbook still parses."""
    from datetime import datetime, timezone

    db_path = tmp_path / "backfill.db"
    workbook = tmp_path / "B999999.xlsx"
    workbook.write_bytes(b"not really a workbook")

    class _Item:
        def __init__(self, mpn: str, find_number: int) -> None:
            self.component_mpn = mpn
            self.find_number = find_number

    class _Result:
        items = [_Item("SCREW-M3", 7), _Item("RC0402", 9)]

    class _FakeBomParser:
        def parse(self, path):
            return _Result()

    registry = ParserRegistry(_FakeBomParser(), None, None, None, None)

    conn = _open(db_path)
    migrate(conn, ParserRegistry(_NullBomParser(), None, None, None, None))

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, "
        "quantity, status, created_at, updated_at) VALUES (1, 'B999999', 'WO1', '', 1, "
        "'Not Clear', ?, ?)",
        (now, now)
    )
    conn.execute(
        "INSERT INTO source_files (id, audit_id, file_category, original_filename, "
        "local_storage_path, file_hash, ingested_at) "
        "VALUES (5, 1, 'BOM', 'B999999.xlsx', ?, 'hash', ?)",
        (str(workbook), now)
    )
    for ref_des, mpn in (("H1", "SCREW-M3"), ("H2", "SCREW-M3"), ("R1", "RC0402")):
        conn.execute(
            "INSERT INTO audit_bom_components "
            "(source_file_id, component_mpn, ref_des, mount_type, description, find_number) "
            "VALUES (5, ?, ?, 'T', NULL, 0)",
            (mpn, ref_des)
        )

    conn.execute("ALTER TABLE audit_bom_components DROP COLUMN find_number")
    conn.execute("UPDATE schema_version SET version = 16 WHERE singleton_guard = 1")

    migrate_to_v17(conn, registry)

    found = {
        (r["ref_des"], r["find_number"])
        for r in conn.execute("SELECT ref_des, find_number FROM audit_bom_components")
    }
    assert found == {("H1", 7), ("H2", 7), ("R1", 9)}


def test_v17_tolerates_an_unreadable_workbook(tmp_path):
    """A missing or corrupt upload leaves 0s rather than blocking startup."""
    from datetime import datetime, timezone

    class _RaisingBomParser:
        def parse(self, path):
            raise ValueError("corrupt")

    registry = ParserRegistry(_RaisingBomParser(), None, None, None, None)

    conn = _open(tmp_path / "tolerant.db")
    migrate(conn, ParserRegistry(_NullBomParser(), None, None, None, None))

    workbook = tmp_path / "B888888.xlsx"
    workbook.write_bytes(b"corrupt")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, "
        "quantity, status, created_at, updated_at) VALUES (1, 'B888888', 'WO1', '', 1, "
        "'Not Clear', ?, ?)",
        (now, now)
    )
    conn.execute(
        "INSERT INTO source_files (id, audit_id, file_category, original_filename, "
        "local_storage_path, file_hash, ingested_at) "
        "VALUES (5, 1, 'BOM', 'B888888.xlsx', ?, 'hash', ?)",
        (str(workbook), now)
    )
    conn.execute(
        "INSERT INTO audit_bom_components "
        "(source_file_id, component_mpn, ref_des, mount_type, description, find_number) "
        "VALUES (5, 'X', 'R1', 'S', NULL, 3)"
    )

    conn.execute("ALTER TABLE audit_bom_components DROP COLUMN find_number")
    conn.execute("UPDATE schema_version SET version = 16 WHERE singleton_guard = 1")

    assert migrate_to_v17(conn, registry) is True
    assert conn.execute("SELECT find_number FROM audit_bom_components").fetchone()["find_number"] == 0
