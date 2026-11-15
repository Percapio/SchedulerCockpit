"""Reading the share in as few calls as the watchdog has to watch (Phase 55b §4.2, §4.3).

No Qt, no database. Every function here that touches the share is meant to be called through a
ShareCallGuard, one call per folder read.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Any, Callable, TypeVar, Union

from cockpit.ingestion.locator import is_job_folder_name, job_parent_directory

T = TypeVar("T")
ShareCallGuard = Callable[[Callable[[], T]], T]


def direct_share_call(call: Callable[[], T]) -> T:
    """The default guard: calls straight through, unwatched (tools and tests)."""
    return call()


@dataclass(frozen=True)
class ShareEntry:
    """One directory entry, with what the enumeration said about it.

    stat_error is set when the entry's metadata couldn't be read; size and modified_ns are then 0.
    """

    name: str
    path: pathlib.Path
    is_dir: bool
    is_file: bool
    size: int
    modified_ns: int
    stat_error: str | None = None


@dataclass(frozen=True)
class FolderMissing:
    folder: pathlib.Path


FolderListing = Union[list[ShareEntry], FolderMissing]


def _kind_of(error: BaseException) -> str:
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    return f"{type(error).__name__} ({code})" if code is not None else type(error).__name__


def list_share_folder(folder: pathlib.Path) -> FolderListing:
    """Reads one folder's entries, each with its kind, size and modified time, in one enumeration.

    pre:  called through share_call
    post: one ShareEntry per directory entry; on Windows the enumeration carries each entry's size and
          time, so no entry is stat'ed; FolderMissing iff the folder doesn't exist
    raises: OSError for any other failure
    """
    try:
        iterator = os.scandir(folder)
    except (FileNotFoundError, NotADirectoryError):
        return FolderMissing(pathlib.Path(folder))
    entries: list[ShareEntry] = []
    with iterator:
        for entry in iterator:
            try:
                info = entry.stat()
                entries.append(ShareEntry(
                    name=entry.name, path=pathlib.Path(entry.path), is_dir=entry.is_dir(),
                    is_file=entry.is_file(), size=info.st_size, modified_ns=info.st_mtime_ns,
                ))
            except OSError as error:
                entries.append(ShareEntry(
                    name=entry.name, path=pathlib.Path(entry.path), is_dir=False, is_file=False,
                    size=0, modified_ns=0, stat_error=_kind_of(error),
                ))
    return entries


class JobFolderIndex:
    """Finds jobs' folders, reading each parent folder at most once per run (Phase 55b §4.3)."""

    def __init__(self, source_root: pathlib.Path, share_call: ShareCallGuard = direct_share_call) -> None:
        self.source_root = pathlib.Path(source_root)
        self.share_call = share_call
        self._listings: dict[pathlib.Path, FolderListing] = {}
        self.listings_read = 0

    def find(self, job_number: str) -> pathlib.Path | FolderMissing:
        """The folder resolve_job_directory would return, or FolderMissing(parent / job_number).

        post: the parent is source_root / "<first 4>xxx" / "<first 5>xx"; the folder is the first child
              directory, in listing order, whose name is_job_folder_name accepts; the parent is listed
              through share_call the first time a job under it is asked for, and kept for the run
        raises: OSError when the parent can't be listed for any reason but absence
        """
        parent = job_parent_directory(job_number, self.source_root)
        if parent not in self._listings:
            self._listings[parent] = self.share_call(lambda: list_share_folder(parent))
            self.listings_read += 1
        listing = self._listings[parent]
        if isinstance(listing, FolderMissing):
            return FolderMissing(parent / job_number)
        for entry in listing:
            if entry.is_dir and is_job_folder_name(entry.name, job_number):
                return parent / entry.name
        return FolderMissing(parent / job_number)


def guarded(share_call: ShareCallGuard | None) -> ShareCallGuard:
    """The guard to use: the one given, else straight through."""
    return share_call if share_call is not None else direct_share_call


def describe_listing_error(error: Any) -> str:
    return _kind_of(error)
