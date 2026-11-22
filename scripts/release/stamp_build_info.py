"""Stamps cockpit/_build_info.py with the build's identity before PyInstaller runs (Patch 14 §10.6).

The build is three commands, from the repository root:

    python scripts/release/stamp_build_info.py --version 1.83
    pyinstaller cockpit.spec
    python scripts/release/stamp_build_info.py --restore

Phase 17 §5.4 left this to a CI pipeline that was never built, so every production build logged
"0.0.0-dev". Restoring needs no git: the sentinel file is written from the copy below, byte for byte, so
the tree is clean afterwards.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import pathlib
import subprocess
import sys
from typing import Callable

BUILD_INFO_PATH = pathlib.Path("cockpit") / "_build_info.py"

SENTINEL_SOURCE = '''"""Build metadata injected by CI."""

from dataclasses import dataclass

@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str
    built_at_utc: str

def get_build_info() -> BuildInfo:
    """Get the build info, using sentinels for source runs."""
    return BuildInfo(
        version="0.0.0-dev",
        commit="source",
        built_at_utc="source"
    )
'''

STAMPED_SOURCE = '''"""Build metadata, stamped by scripts/release/stamp_build_info.py. Restore with --restore after the build."""

from dataclasses import dataclass

@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str
    built_at_utc: str

def get_build_info() -> BuildInfo:
    """The identity of this build."""
    return BuildInfo(
        version={version!r},
        commit={commit!r},
        built_at_utc={built_at!r}
    )
'''


class BuildStampFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str
    built_at_utc: str


SENTINEL = BuildInfo("0.0.0-dev", "source", "source")


def _git(repository: pathlib.Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments], cwd=repository, capture_output=True, text=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BuildStampFailed(f"git {' '.join(arguments)} failed: {error}") from error
    return completed.stdout.strip()


def _write(path: pathlib.Path, text: str) -> None:
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as error:
        raise BuildStampFailed(f"can't write {path}: {error}") from error


def stamp_build_info(
    version: str,
    repository: pathlib.Path,
    clock: Callable[[], datetime.datetime],
    restore: bool,
) -> BuildInfo:
    """Writes the build's identity into cockpit/_build_info.py, or puts the sentinel back.

    pre:  repository is the repository root; stamping needs git, restoring doesn't
    post: restore=false: the file returns version, commit (short HEAD, "-dirty" when the tree has
          uncommitted changes other than this file) and built_at_utc (now), and that BuildInfo is returned;
          a stamp left by a failed build is simply replaced and never counts toward "-dirty".
          restore=true: the sentinel file is written from SENTINEL_SOURCE and SENTINEL is returned
    raises: BuildStampFailed when git isn't available for a stamp, or the file can't be written
    """
    path = repository / BUILD_INFO_PATH
    if restore:
        _write(path, SENTINEL_SOURCE)
        return SENTINEL
    if not version.strip():
        raise BuildStampFailed("a version is required, for example --version 1.83")
    commit = _git(repository, "rev-parse", "--short", "HEAD")
    changes = _git(repository, "status", "--porcelain", "--", ".", f":(exclude){BUILD_INFO_PATH.as_posix()}")
    if changes:
        commit += "-dirty"
    built_at = clock().astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    info = BuildInfo(version.strip(), commit, built_at)
    _write(path, STAMPED_SOURCE.format(version=info.version, commit=info.commit, built_at=info.built_at_utc))
    return info


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Stamp or restore cockpit/_build_info.py")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--version", help="the version being built, for example 1.83")
    group.add_argument("--restore", action="store_true", help="put the sentinel file back")
    arguments = parser.parse_args(argv)
    repository = pathlib.Path.cwd()
    try:
        info = stamp_build_info(
            arguments.version or "", repository, lambda: datetime.datetime.now(datetime.timezone.utc),
            restore=arguments.restore,
        )
    except BuildStampFailed as error:
        print(f"stamp_build_info: {error.reason}", file=sys.stderr)
        return 1
    print(f"{BUILD_INFO_PATH.as_posix()}: version={info.version} commit={info.commit} built_at_utc={info.built_at_utc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
