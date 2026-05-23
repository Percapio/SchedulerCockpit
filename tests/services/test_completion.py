import os
import pathlib
import pytest
import sqlite3

from cockpit.persistence.types import AuditStatus, SourceFileCategory
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.services.storage_reaper import StorageReaper
from cockpit.services.completion import CompletionService, CleanupFailedError
from cockpit.services.startup_reconciler import StartupReconciler
from cockpit.ingestion.hashing import sha256_hex
from cockpit.persistence.errors import PersistenceError, IncompleteChecklistError
from cockpit.persistence.schema import migrate_to_v1


def test_hard_delete_failure_leaves_completed(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate_to_v1(conn)
        
    audit_repo = AuditRepository(conn)
    source_file_repo = SourceFileRepository(conn)
    
    # insert an audit
    conn.execute("INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) VALUES (1, 'p', 'w', '', 1, 'InProgress', '2020', '2020')")
    # complete checklists so transition works
    conn.execute("INSERT INTO tht_verification_checklist (audit_id, component_mpn, is_verified) VALUES (1, 'c', 1)")
    
    storage_reaper = StorageReaper(source_file_repo)
    completion_service = CompletionService(conn, audit_repo, source_file_repo, storage_reaper)
    
    def mock_hard_delete(audit_id):
        raise PersistenceError("Simulated kill")
        
    monkeypatch.setattr(audit_repo, "hard_delete", mock_hard_delete)
    
    with pytest.raises(PersistenceError, match="Simulated kill"):
        completion_service.complete_and_cleanup(1)
        
    # verify status is Completed
    cur = conn.cursor()
    cur.execute("SELECT status FROM active_audits WHERE id = 1")
    assert cur.fetchone()[0] == AuditStatus.COMPLETED.value


def test_startup_reconciler_orphan_file(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate_to_v1(conn)
        
    audit_repo = AuditRepository(conn)
    source_file_repo = SourceFileRepository(conn)
    storage_reaper = StorageReaper(source_file_repo)
    completion_service = CompletionService(conn, audit_repo, source_file_repo, storage_reaper)
    
    file_storage_root = tmp_path / "cockpit_data"
    file_storage_root.mkdir()
    
    # create orphan
    orphan_dir = file_storage_root / "orphan-test"
    orphan_dir.mkdir()
    orphan_file = orphan_dir / "garbage.xlsx"
    orphan_file.write_text("garbage")
    
    reconciler = StartupReconciler(
        audit_repo, source_file_repo, completion_service, file_storage_root, sha256_hex
    )
    report = reconciler.reconcile()
    
    assert len(report.orphans_deleted) == 1
    assert not orphan_file.exists()
    assert not orphan_dir.exists()


def test_incomplete_checklist_rollback(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate_to_v1(conn)
        
    audit_repo = AuditRepository(conn)
    source_file_repo = SourceFileRepository(conn)
    storage_reaper = StorageReaper(source_file_repo)
    completion_service = CompletionService(conn, audit_repo, source_file_repo, storage_reaper)
    
    # insert an audit
    conn.execute("INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) VALUES (1, 'p', 'w', '', 1, 'InProgress', '2020', '2020')")
    # leave checklist incomplete
    conn.execute("INSERT INTO tht_verification_checklist (audit_id, component_mpn, is_verified) VALUES (1, 'c', 0)")
    
    with pytest.raises(IncompleteChecklistError):
        completion_service.complete_and_cleanup(1)
        
    # verify status is still InProgress
    cur = conn.cursor()
    cur.execute("SELECT status FROM active_audits WHERE id = 1")
    assert cur.fetchone()[0] == AuditStatus.IN_PROGRESS.value
