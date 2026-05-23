"""Startup reconciler."""

import os
import pathlib
from typing import Callable

from cockpit.persistence.errors import PersistenceError
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.services.completion import CompletionService, CleanupFailedError
from cockpit.services.views import ReconciliationReport


class StartupReconciler:
    def __init__(
        self,
        audit_repo: AuditRepository,
        source_file_repo: SourceFileRepository,
        completion_service: CompletionService,
        file_storage_root: pathlib.Path,
        hash_for_path: Callable[[pathlib.Path], str],
    ) -> None:
        self._audit_repo = audit_repo
        self._source_file_repo = source_file_repo
        self._completion_service = completion_service
        self._file_storage_root = file_storage_root
        self._hash_for_path = hash_for_path

    def reconcile(self) -> ReconciliationReport:
        cleaned = []
        partial = []
        errors = []
        orphans_deleted = []
        orphan_delete_failed = []
        unreadable = []
        pruned = []
        notes = []

        # 1. Stranded-row sweep
        stranded_audits = self._audit_repo.list_completed()
        for audit in stranded_audits:
            try:
                outcome = self._completion_service.cleanup_already_completed(audit.id)
                cleaned.append(outcome)
            except CleanupFailedError as exc:
                partial.append((audit.id, exc.reap_report))
            except PersistenceError as exc:
                errors.append((audit.id, exc))

        # 2. Orphan-file sweep
        if not self._file_storage_root.exists():
            notes.append(f"Storage root {self._file_storage_root} does not exist, skipping orphan sweep.")
        else:
            directories_to_check = set()
            for root, dirs, files in os.walk(self._file_storage_root):
                root_path = pathlib.Path(root)
                directories_to_check.add(root_path)
                
                for f in files:
                    path = root_path / f
                    try:
                        h = self._hash_for_path(path)
                    except OSError as exc:
                        unreadable.append((path, exc))
                        continue
                        
                    if self._source_file_repo.reference_count(h) == 0:
                        try:
                            path.unlink()
                            orphans_deleted.append(path)
                        except OSError as exc:
                            orphan_delete_failed.append((path, exc))

            # Prune empty directories (post order)
            # sort in reverse to do deepest first
            sorted_dirs = sorted(directories_to_check, key=lambda p: len(p.parts), reverse=True)
            for d in sorted_dirs:
                if d == self._file_storage_root:
                    continue # don't delete root
                try:
                    d.rmdir()
                    pruned.append(d)
                except OSError:
                    pass

        return ReconciliationReport(
            cleaned=cleaned,
            partial=partial,
            errors=errors,
            orphans_deleted=orphans_deleted,
            orphan_delete_failed=orphan_delete_failed,
            unreadable=unreadable,
            pruned=pruned,
            notes=notes
        )
