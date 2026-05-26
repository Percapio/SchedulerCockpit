"""Storage reaper."""

import pathlib

from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.types import SourceFile
from cockpit.services.views import ReapReport
import logging
logger = logging.getLogger(__name__)



class StorageReaper:
    def __init__(self, source_file_repo: SourceFileRepository) -> None:
        self._source_file_repo = source_file_repo

    def reap(self, captured_files: list[SourceFile]) -> ReapReport:
        deleted_paths = []
        retained_files = []
        failed_paths = []
        directories_to_check = set()
        pruned_directories = []

        for sf in captured_files:
            path = sf.local_storage_path
            directories_to_check.add(path.parent)

            count = self._source_file_repo.reference_count(sf.file_hash)
            if count == 0:
                try:
                    path.unlink()
                    deleted_paths.append(path)
                except FileNotFoundError:
                    logger.exception('Exception caught in storage_reaper')
                    # Already gone, treat as success
                    deleted_paths.append(path)
                except OSError as e:
                    logger.exception('Exception caught in storage_reaper')
                    failed_paths.append((path, str(e)))
            else:
                retained_files.append((path, sf.file_hash))

        for d in directories_to_check:
            if d.exists() and d.is_dir():
                try:
                    d.rmdir()
                    pruned_directories.append(d)
                except OSError:
                    logger.exception('Exception caught in storage_reaper')
                    # Fails if not empty, expected behavior
                    pass

        return ReapReport(
            deleted_paths=deleted_paths,
            retained_files=retained_files,
            failed_paths=failed_paths,
            pruned_directories=pruned_directories
        )
