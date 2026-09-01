"""Application data reset service (Phase 43)."""

import enum
import logging
import pathlib
import sqlite3
from dataclasses import dataclass

from cockpit.persistence.errors import PersistenceError
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.services.storage_reaper import StorageReaper
from cockpit.persistence.types import SourceFile


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnreapedFile:
    path: pathlib.Path
    reason: str


@dataclass(frozen=True)
class ResetOutcome:
    audits_deleted: int
    files_deleted: int
    unreaped: list[UnreapedFile]
    pruned_directories: list[pathlib.Path]


class ResetAbortCause(enum.Enum):
    CAPTURE_FAILED = "CAPTURE_FAILED"
    COUNT_MISMATCH = "COUNT_MISMATCH"
    DELETE_FAILED = "DELETE_FAILED"


@dataclass(frozen=True)
class ResetAborted:
    cause: ResetAbortCause
    observed_count: int | None = None
    underlying: PersistenceError | None = None


def capture_all_owned_files(
    audit_repo: AuditRepository,
    source_file_repo: SourceFileRepository
) -> list[SourceFile]:
    """Capture all source files owned by any active audit, deduplicated by path."""
    try:
        active_ids = audit_repo.all_active_ids()
    except sqlite3.Error as e:
        raise PersistenceError(str(e)) from e

    unique_paths = set()
    deduped_files = []

    for audit_id in active_ids:
        try:
            files = source_file_repo.list_for_audit(audit_id)
        except sqlite3.Error as e:
            raise PersistenceError(str(e)) from e
        for f in files:
            path_str = str(f.local_storage_path)
            if path_str not in unique_paths:
                unique_paths.add(path_str)
                deduped_files.append(f)

    return deduped_files


def reset_application_data(
    audit_repo: AuditRepository,
    source_file_repo: SourceFileRepository,
    storage_reaper: StorageReaper,
    connection: sqlite3.Connection,
    expected_audit_count: int
) -> ResetOutcome | ResetAborted:
    """
    Deletes every active audit and reaps the files they own.
    All-or-nothing on the database; best-effort on the filesystem.
    """
    try:
        captured_files = capture_all_owned_files(audit_repo, source_file_repo)
    except PersistenceError as exc:
        return ResetAborted(ResetAbortCause.CAPTURE_FAILED, underlying=exc)

    cursor = connection.cursor()

    try:
        cursor.execute("SAVEPOINT reset_app_data")
        
        current_count = len(audit_repo.all_active_ids())
        if current_count != expected_audit_count:
            cursor.execute("ROLLBACK TO SAVEPOINT reset_app_data")
            return ResetAborted(ResetAbortCause.COUNT_MISMATCH, observed_count=current_count)

        cursor.execute("DELETE FROM active_audits")
        cursor.execute("RELEASE SAVEPOINT reset_app_data")
    except sqlite3.Error as exc:
        cursor.execute("ROLLBACK TO SAVEPOINT reset_app_data")
        return ResetAborted(ResetAbortCause.DELETE_FAILED, underlying=PersistenceError(str(exc)))
    except PersistenceError as exc:
        cursor.execute("ROLLBACK TO SAVEPOINT reset_app_data")
        return ResetAborted(ResetAbortCause.DELETE_FAILED, underlying=exc)

    reap_report = storage_reaper.reap(captured_files)
    
    unreaped = []
    for path, reason in reap_report.failed_paths:
        unreaped.append(UnreapedFile(path=path, reason=reason))
        
    outcome = ResetOutcome(
        audits_deleted=expected_audit_count,
        files_deleted=len(reap_report.deleted_paths),
        unreaped=unreaped,
        pruned_directories=list(reap_report.pruned_directories)
    )

    try:
        cursor.execute("VACUUM")
    except sqlite3.Error:
        logger.warning("VACUUM failed after data reset", exc_info=True)

    return outcome
