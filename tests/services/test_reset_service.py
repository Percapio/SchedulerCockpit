"""Tests for application data reset service."""

import pathlib
import pytest
import sqlite3

from cockpit.persistence.connection import open_connection
from cockpit.persistence.errors import PersistenceError
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.types import ActiveAuditDraft, SourceFileDraft, SourceFileCategory
from cockpit.services.reset_service import (
    reset_application_data, ResetOutcome, ResetAborted, ResetAbortCause, capture_all_owned_files
)
from cockpit.services.storage_reaper import StorageReaper

@pytest.fixture
def db_conn(tmp_path):
    conn = open_connection(tmp_path / "test.db")
    from cockpit.persistence.schema import migrate
    from cockpit.protocols import ParserRegistry
    # minimal dummy registry to pass migration
    class DummyRegistry:
        eco_parser = None
        traveler_parser = None
        bom_parser = None
        pdf_layout_parser = None
        coord_map = {}
    migrate(conn, DummyRegistry())
    
    # insert dummy holiday and schema_version to ensure they survive
    cur = conn.cursor()
    cur.execute("INSERT INTO holidays (holiday_date) VALUES ('2026-12-25')")
    # schema_version is already seeded by migrate()
    yield conn
    conn.close()


@pytest.fixture
def repos(db_conn):
    bom_repo = AuditBomComponentRepository(db_conn)
    pdf_repo = PdfComponentCoordRepository(db_conn)
    audit_repo = AuditRepository(db_conn, bom_repo, pdf_repo)
    source_file_repo = SourceFileRepository(db_conn)
    return audit_repo, source_file_repo


@pytest.fixture
def storage_reaper(repos):
    return StorageReaper(repos[1])


def test_reset_with_zero_audits(repos, storage_reaper, db_conn):
    audit_repo, source_file_repo = repos
    outcome = reset_application_data(audit_repo, source_file_repo, storage_reaper, db_conn, 0)
    assert isinstance(outcome, ResetOutcome)
    assert outcome.audits_deleted == 0
    assert outcome.files_deleted == 0
    assert not outcome.unreaped


def test_reset_with_n_unrelated_audits(repos, storage_reaper, db_conn, tmp_path):
    audit_repo, source_file_repo = repos
    
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    f1 = upload_dir / "file1.txt"
    f1.write_text("1")
    f2 = upload_dir / "file2.txt"
    f2.write_text("2")
    
    a1 = audit_repo.create(ActiveAuditDraft("P1", "W1", "-A", 1, None, "sch1"))
    a2 = audit_repo.create(ActiveAuditDraft("P2", "W2", "-A", 1, None, "sch2"))
    
    source_file_repo.register(SourceFileDraft(a1.id, SourceFileCategory.BOM.value, "f1.txt", f1, "a"*64))
    source_file_repo.register(SourceFileDraft(a2.id, SourceFileCategory.BOM.value, "f2.txt", f2, "b"*64))
    
    outcome = reset_application_data(audit_repo, source_file_repo, storage_reaper, db_conn, 2)
    
    assert isinstance(outcome, ResetOutcome)
    assert outcome.audits_deleted == 2
    assert outcome.files_deleted == 2
    assert not f1.exists()
    assert not f2.exists()
    assert len(audit_repo.list_open()) == 0


def test_reset_with_split_pair_sharing_one_file(repos, storage_reaper, db_conn, tmp_path):
    audit_repo, source_file_repo = repos
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    f1 = upload_dir / "file1.txt"
    f1.write_text("1")
    
    a1 = audit_repo.create(ActiveAuditDraft("P1", "W1", "-A", 1, None, "sch1"))
    source_file_repo.register(SourceFileDraft(a1.id, SourceFileCategory.BOM.value, "f1.txt", f1, "a"*64))
    
    # split shares file
    a2 = audit_repo.clone_to_suffix(a1.id, "-B", 1, "split")
    
    outcome = reset_application_data(audit_repo, source_file_repo, storage_reaper, db_conn, 2)
    
    assert isinstance(outcome, ResetOutcome)
    assert outcome.audits_deleted == 2
    assert outcome.files_deleted == 1
    assert not f1.exists()


def test_untracked_file_survives_reset(repos, storage_reaper, db_conn, tmp_path):
    audit_repo, source_file_repo = repos
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    untracked = upload_dir / "untracked.txt"
    untracked.write_text("unt")
    
    a1 = audit_repo.create(ActiveAuditDraft("P1", "W1", "-A", 1, None, "sch1"))
    
    outcome = reset_application_data(audit_repo, source_file_repo, storage_reaper, db_conn, 1)
    
    assert isinstance(outcome, ResetOutcome)
    assert untracked.exists()


def test_cascade_coverage_and_retained_tables(repos, storage_reaper, db_conn, tmp_path):
    audit_repo, source_file_repo = repos
    a1 = audit_repo.create(ActiveAuditDraft("P1", "W1", "-A", 1, None, "sch1"))
    
    # insert a source file to check cascade
    f = tmp_path / "f.txt"
    f.write_text("x")
    source_file_repo.register(SourceFileDraft(a1.id, SourceFileCategory.BOM.value, "f.txt", f, "a"*64))
    
    reset_application_data(audit_repo, source_file_repo, storage_reaper, db_conn, 1)
    
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM source_files")
    assert cur.fetchone()["c"] == 0
    
    cur.execute("SELECT COUNT(*) as c FROM holidays")
    assert cur.fetchone()["c"] > 0
    
    cur.execute("SELECT COUNT(*) as c FROM schema_version")
    assert cur.fetchone()["c"] > 0


def test_capture_raises_aborts(repos, storage_reaper, db_conn):
    audit_repo, source_file_repo = repos
    a1 = audit_repo.create(ActiveAuditDraft("P1", "W1", "-A", 1, None, "sch1"))
    
    # simulate capture failure
    class BrokenSourceRepo:
        def list_for_audit(self, audit_id):
            raise sqlite3.Error("db disconnected")
    
    result = reset_application_data(audit_repo, BrokenSourceRepo(), storage_reaper, db_conn, 1)
    
    assert isinstance(result, ResetAborted)
    assert result.cause == ResetAbortCause.CAPTURE_FAILED
    assert len(audit_repo.list_open()) == 1


def test_delete_raises_aborts_and_rolls_back(repos, storage_reaper, db_conn, tmp_path):
    audit_repo, source_file_repo = repos
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    f1 = upload_dir / "file1.txt"
    f1.write_text("1")
    
    a1 = audit_repo.create(ActiveAuditDraft("P1", "W1", "-A", 1, None, "sch1"))
    source_file_repo.register(SourceFileDraft(a1.id, SourceFileCategory.BOM.value, "f1.txt", f1, "a"*64))
    
    class BrokenConnection(sqlite3.Connection):
        def cursor(self):
            class BrokenCursor:
                def execute(self, sql, *args):
                    if "DELETE FROM active_audits" in sql:
                        raise sqlite3.Error("disk full")
                    return db_conn.cursor().execute(sql, *args)
            return BrokenCursor()

    # Need a way to inject a failing DELETE without breaking SAVEPOINT logic
    # We can mock the connection or use an active_audits trigger that throws
    db_conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON active_audits BEGIN SELECT RAISE(ABORT, 'forced failure'); END;")
    
    result = reset_application_data(audit_repo, source_file_repo, storage_reaper, db_conn, 1)
    
    assert isinstance(result, ResetAborted)
    assert result.cause == ResetAbortCause.DELETE_FAILED
    assert len(audit_repo.list_open()) == 1
    assert f1.exists()


def test_count_mismatch_aborts(repos, storage_reaper, db_conn):
    audit_repo, source_file_repo = repos
    a1 = audit_repo.create(ActiveAuditDraft("P1", "W1", "-A", 1, None, "sch1"))
    
    result = reset_application_data(audit_repo, source_file_repo, storage_reaper, db_conn, 0) # Expected 0, found 1
    
    assert isinstance(result, ResetAborted)
    assert result.cause == ResetAbortCause.COUNT_MISMATCH
    assert result.observed_count == 1
    assert len(audit_repo.list_open()) == 1
