"""Logs and crash reports are kept for a stated window (Patch 15 §8, F26).

The weekly log handler deletes its own dated files beyond the window. This module deletes what it doesn't own:
1.84's size-rotated cockpit.log.N, the install-folder duplicate's legacy files, and crash reports, which the
crash sink otherwise caps only by count (the 50 newest per folder).
"""

from __future__ import annotations

import collections
import logging
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

ROTATED_LOG_NAME = re.compile(r"^cockpit\.log\..+$")
DUPLICATE_LOG_NAME = re.compile(r"^log\.txt\..+$")
# The crash reporter names its files "<occurred_at_utc with ':' as '-'>-<class>.json".
CRASH_REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T.+\.json$")
DAY_S = 86_400


@dataclass(frozen=True)
class PruneTarget:
    folder: pathlib.Path
    name_pattern: "re.Pattern[str]"


@dataclass(frozen=True)
class RetentionReport:
    deleted: int
    failed: int


def retention_targets(
    log_dir: pathlib.Path,
    crash_dir: pathlib.Path,
    data_root: pathlib.Path,
    duplicate_dir: Optional[pathlib.Path] = None,
) -> list[PruneTarget]:
    """The folders prune_by_age looks in, with the names it owns there (Patch 15 §8's table)."""
    targets = [
        PruneTarget(log_dir, ROTATED_LOG_NAME),
        PruneTarget(crash_dir, CRASH_REPORT_NAME),
        PruneTarget(data_root, CRASH_REPORT_NAME),
    ]
    if duplicate_dir is not None:
        targets.append(PruneTarget(duplicate_dir, DUPLICATE_LOG_NAME))
    return targets


def prune_by_age(targets: Sequence[PruneTarget], retention_days: int, now: float) -> RetentionReport:
    """Deletes logs and crash reports older than the retention window (Patch 15 §8).

    pre:  logging is configured; any target folder may be absent
    post: in each target, every regular file whose name matches the target's pattern and whose modification time
          is before now - retention_days is deleted. The live cockpit.log and log.txt never match. One INFO line
          reports the counts. A file that can't be deleted is counted in failed, and its error class is logged at
          WARNING without its path
    raises: nothing
    """
    cutoff = now - retention_days * DAY_S
    deleted = failed = 0
    failures: collections.Counter[str] = collections.Counter()
    seen: set[str] = set()
    for target in targets:
        try:
            key = os.path.normcase(str(target.folder.resolve()))
        except OSError:
            key = os.path.normcase(str(target.folder))
        if key + target.name_pattern.pattern in seen:
            continue
        seen.add(key + target.name_pattern.pattern)
        try:
            entries = list(target.folder.iterdir())
        except OSError:
            continue
        for path in entries:
            if not target.name_pattern.match(path.name):
                continue
            try:
                if not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                deleted += 1
            except OSError as error:
                failed += 1
                failures[type(error).__name__] += 1
    for name, count in sorted(failures.items()):
        logger.warning("Retention: %d file(s) couldn't be deleted (%s)", count, name)
    logger.info(
        "Retention: deleted %d log and crash-report file(s) older than %d days; %d failed",
        deleted, retention_days, failed,
    )
    return RetentionReport(deleted, failed)
