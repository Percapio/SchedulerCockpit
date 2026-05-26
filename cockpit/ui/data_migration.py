"""Data migration to versioned layout."""

import datetime
import json
import os
import pathlib
import sys
from dataclasses import dataclass
import logging
logger = logging.getLogger(__name__)


class DataMigrationError(Exception):
    pass

@dataclass(frozen=True)
class MigrationOutcome:
    performed: bool
    moved_items: tuple[pathlib.Path, ...]
    legacy_root: pathlib.Path
    target_root: pathlib.Path

def migrate_to_versioned_layout(app_data_root: pathlib.Path, version_dir_name: str = "v1") -> MigrationOutcome:
    """Migrate legacy data into a versioned subdirectory using an exclusive lock."""
    v1_dir = app_data_root / version_dir_name
    sentinel_path = v1_dir / ".migration_complete"
    
    if sentinel_path.exists():
        return MigrationOutcome(False, (), app_data_root, v1_dir)
        
    legacy_names = ["bootstrap_error.txt", "uploads", "logs", "local_audit.db"]
    legacy_files = [app_data_root / name for name in legacy_names if (app_data_root / name).exists()]
    
    if not legacy_files and not v1_dir.exists():
        v1_dir.mkdir(parents=True, exist_ok=True)
        _write_sentinel(sentinel_path, [])
        return MigrationOutcome(True, (), app_data_root, v1_dir)
        
    if legacy_files and not sentinel_path.exists() and v1_dir.exists():
        raise DataMigrationError(
            f"Conflict: {v1_dir} exists without sentinel but legacy files are present in {app_data_root}"
        )
        
    app_data_root.mkdir(parents=True, exist_ok=True)
    lock_path = app_data_root / ".migration.lock"
    
    # Acquire lock
    if sys.platform == "win32":
        import msvcrt
        with open(lock_path, "w") as lock_file:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                return _perform_migration(app_data_root, v1_dir, sentinel_path, legacy_files)
            except OSError as e:
                logger.exception('Exception caught in data_migration')
                raise DataMigrationError("Failed to acquire migration lock") from e
    else:
        # Fallback for non-Windows (tests) using fcntl
        import fcntl
        with open(lock_path, "w") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                return _perform_migration(app_data_root, v1_dir, sentinel_path, legacy_files)
            except OSError as e:
                logger.exception('Exception caught in data_migration')
                raise DataMigrationError("Failed to acquire migration lock") from e
                
def _perform_migration(app_data_root: pathlib.Path, v1_dir: pathlib.Path, sentinel_path: pathlib.Path, legacy_files: list[pathlib.Path]) -> MigrationOutcome:
    v1_dir.mkdir(parents=True, exist_ok=True)
    
    # Order matters: local_audit.db last
    ordered_files = []
    for name in ["bootstrap_error.txt", "uploads", "logs", "local_audit.db"]:
        src = app_data_root / name
        if src in legacy_files:
            ordered_files.append(src)
            
    moved_items = []
    moved_names = []
    
    for src in ordered_files:
        dst = v1_dir / src.name
        try:
            os.replace(str(src), str(dst))
            moved_items.append(dst)
            moved_names.append(src.name)
        except OSError as e:
            logger.exception('Exception caught in data_migration')
            raise DataMigrationError(f"Failed to move {src.name} to {dst}") from e
            
    _write_sentinel(sentinel_path, moved_names)
    
    # cleanup lock file is handled by context manager unlocking, we can also delete it
    lock_path = app_data_root / ".migration.lock"
    try:
        if lock_path.exists():
            lock_path.unlink()
    except OSError:
        logger.exception('Exception caught in data_migration')
        pass
        
    return MigrationOutcome(True, tuple(moved_items), app_data_root, v1_dir)

def _write_sentinel(path: pathlib.Path, moved_names: list[str]) -> None:
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    data = {
        "migrated_at": now_utc,
        "from_layout": "flat",
        "moved": moved_names
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
