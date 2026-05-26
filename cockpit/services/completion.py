"""Completion service."""

import sqlite3

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.types import AuditStatus

from cockpit.services.storage_reaper import StorageReaper
from cockpit.services.views import CompletionOutcome, ReapReport
import logging
logger = logging.getLogger(__name__)



class CleanupFailedError(Exception):
    def __init__(self, audit_id: int, reap_report: ReapReport) -> None:
        super().__init__(f"Audit {audit_id}: file cleanup partially failed")
        self.audit_id = audit_id
        self.reap_report = reap_report


class CompletionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit_repo: AuditRepository,
        source_file_repo: SourceFileRepository,
        storage_reaper: StorageReaper,
    ) -> None:
        self._conn = conn
        self._audit_repo = audit_repo
        self._source_file_repo = source_file_repo
        self._storage_reaper = storage_reaper

    def complete_and_cleanup(self, audit_id: int) -> CompletionOutcome:
        captured = self._source_file_repo.list_for_audit(audit_id)
        
        self._audit_repo.transition_status(audit_id, AuditStatus.COMPLETED)
        
        cur = self._conn.cursor()
        cur.execute("SAVEPOINT completion")
        try:
            self._audit_repo.hard_delete(audit_id)
            cur.execute("RELEASE SAVEPOINT completion")
        except Exception:
            logger.exception('Exception caught in completion')
            cur.execute("ROLLBACK TO SAVEPOINT completion")
            cur.execute("RELEASE SAVEPOINT completion")
            raise

        report = self._storage_reaper.reap(captured)
        
        if report.failed_paths:
            raise CleanupFailedError(audit_id, report)
            
        return CompletionOutcome(audit_id, report)

    def cleanup_already_completed(self, audit_id: int) -> CompletionOutcome:
        captured = self._source_file_repo.list_for_audit(audit_id)
        
        self._audit_repo.hard_delete(audit_id)
        
        report = self._storage_reaper.reap(captured)
        
        if report.failed_paths:
            raise CleanupFailedError(audit_id, report)
            
        return CompletionOutcome(audit_id, report)
