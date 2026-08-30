import pytest
import sqlite3
from cockpit.persistence.connection import hydrating_row_factory
from cockpit.persistence.schema import (
    migrate,
    migrate_to_v1,
    migrate_to_v2,
    migrate_to_v3,
    migrate_to_v4,
    migrate_to_v5,
    migrate_to_v6,
    migrate_to_v7,
    migrate_to_v8,
    migrate_to_v9,
    migrate_to_v10,
    migrate_to_v11,
    migrate_to_v12,
    migrate_to_v13,
)
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.errors import AuditNotFound
from cockpit.protocols import ParserRegistry
from cockpit.persistence.types import AuditStatus, SourceFileCategory


class DummyParser:
    def parse(self, path):
        return None


@pytest.fixture
def dummy_registry():
    return ParserRegistry(DummyParser(), None, None, None, None)


def test_schema_v13_initialization_and_constraints(tmp_path, dummy_registry):
    db_path = tmp_path / "test_init.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory
    migrate(conn, dummy_registry)

    # Check version is 16
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_version WHERE singleton_guard = 1")
    assert cur.fetchone()["version"] == 16

    # Test inserting new statuses (FSU, AOI, OPS)
    for status in [AuditStatus.FSU, AuditStatus.AOI, AuditStatus.OPS]:
        conn.execute(
            "INSERT INTO active_audits (part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
            f"VALUES ('p_{status}', 'w', '', 10, '{status}', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )

    # Test invalid status rejected by CHECK constraint
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO active_audits (part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
            "VALUES ('p_invalid', 'w', '', 10, 'Ready-to-Run', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )

    # Get audit id for source_files test
    cur.execute("SELECT id FROM active_audits LIMIT 1")
    audit_id = cur.fetchone()["id"]

    # Test inserting SecondaryPDF
    conn.execute(
        "INSERT INTO source_files (audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        f"VALUES ({audit_id}, '{SourceFileCategory.SECONDARY_PDF}', 'sec.pdf', '/tmp/sec.pdf', 'hash123', '2026-01-01T00:00:00')"
    )

    # Test invalid file category rejected by CHECK constraint
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_files (audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
            f"VALUES ({audit_id}, 'InvalidCategory', 'sec.pdf', '/tmp/sec.pdf', 'hash999', '2026-01-01T00:00:00')"
        )


def test_v12_v13_migration_from_v11(tmp_path, dummy_registry):
    db_path = tmp_path / "test_migration.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory

    # Migrate up to v11
    migrate_to_v1(conn)
    migrate_to_v2(conn)
    migrate_to_v3(conn, dummy_registry)
    migrate_to_v4(conn)
    migrate_to_v5(conn)
    migrate_to_v6(conn)
    migrate_to_v7(conn)
    migrate_to_v8(conn)
    migrate_to_v9(conn)
    migrate_to_v10(conn)
    migrate_to_v11(conn)

    # Insert an audit with Ready-to-Run status in v11
    conn.execute(
        "INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (100, 'p_ready', 'w_ready', '', 50, 'Ready-to-Run', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source_files (id, audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (200, 100, 'PDF', 'primary.pdf', '/tmp/primary.pdf', 'primary_hash', '2026-01-01T00:00:00')"
    )

    # Now execute migrate_to_v12 and migrate_to_v13
    assert migrate_to_v12(conn) is True
    assert migrate_to_v12(conn) is False  # Idempotent check
    assert migrate_to_v13(conn) is True
    assert migrate_to_v13(conn) is False  # Idempotent check

    cur = conn.cursor()
    cur.execute("SELECT status, is_labeled, are_photos_uploaded FROM active_audits WHERE id = 100")
    row = cur.fetchone()
    assert row["status"] == "Not Clear"
    assert row["is_labeled"] == 0
    assert row["are_photos_uploaded"] == 0

    # Verify foreign key reference still works and SecondaryPDF can be inserted
    conn.execute(
        "INSERT INTO source_files (audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (100, 'SecondaryPDF', 'secondary.pdf', '/tmp/sec.pdf', 'sec_hash', '2026-01-01T00:00:00')"
    )
    cur.execute("SELECT COUNT(*) as cnt FROM source_files WHERE audit_id = 100")
    assert cur.fetchone()["cnt"] == 2


def test_repository_mutators_is_labeled_and_photos(tmp_path, dummy_registry):
    db_path = tmp_path / "test_repo.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory
    migrate(conn, dummy_registry)

    bom_repo = AuditBomComponentRepository(conn)
    pdf_repo = PdfComponentCoordRepository(conn)
    repo = AuditRepository(conn, bom_repo, pdf_repo)

    conn.execute(
        "INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (1, 'p', 'w', '', 100, 'Not Clear', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )

    audit = repo.find_by_id(1)
    assert audit.is_labeled is False
    assert audit.are_photos_uploaded is False

    repo.set_is_labeled(1, True)
    audit = repo.find_by_id(1)
    assert audit.is_labeled is True
    assert audit.are_photos_uploaded is False

    repo.set_are_photos_uploaded(1, True)
    audit = repo.find_by_id(1)
    assert audit.is_labeled is True
    assert audit.are_photos_uploaded is True

    repo.set_is_labeled(1, False)
    audit = repo.find_by_id(1)
    assert audit.is_labeled is False
    assert audit.are_photos_uploaded is True

    with pytest.raises(AuditNotFound):
        repo.set_is_labeled(999, True)
    with pytest.raises(AuditNotFound):
        repo.set_are_photos_uploaded(999, True)


def test_v12_migration_with_missing_v11_columns(tmp_path, dummy_registry):
    """Simulates migrating a v11 database where ops_per_board_min was never added due to dev builds."""
    db_path = tmp_path / "test_missing_col.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory

    # Migrate up to v10
    migrate_to_v1(conn)
    migrate_to_v2(conn)
    migrate_to_v3(conn, dummy_registry)
    migrate_to_v4(conn)
    migrate_to_v5(conn)
    migrate_to_v6(conn)
    migrate_to_v7(conn)
    migrate_to_v8(conn)
    migrate_to_v9(conn)
    migrate_to_v10(conn)

    # Force schema version to 11 without executing v11 DDL (simulating old dev build)
    conn.execute("UPDATE schema_version SET version = 11 WHERE singleton_guard = 1")

    # Insert an audit (ops_per_board_min does not exist in active_audits table here)
    conn.execute(
        "INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (101, 'p_dev', 'w_dev', '', 20, 'Not Clear', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )

    # Now execute migrate_to_v12 - it should pre-create missing columns and succeed without errors
    assert migrate_to_v12(conn) is True
    assert migrate_to_v13(conn) is True

    cur = conn.cursor()
    cur.execute("SELECT ops_per_board_min, is_labeled, are_photos_uploaded FROM active_audits WHERE id = 101")
    row = cur.fetchone()
    assert row["ops_per_board_min"] is None
    assert row["is_labeled"] == 0
    assert row["are_photos_uploaded"] == 0
