"""Typed failures for the AOI XY export (Phase 52 section 7)."""

from pathlib import Path
from typing import Optional


class AoiError(Exception):
    """Base class for cockpit.services.aoi failures."""


class XyFileTooLarge(AoiError):
    def __init__(self, path: Path, size: int, limit: int):
        super().__init__(f"XY file is {size:,} bytes; the limit is {limit:,}: {path}")
        self.path = path
        self.size = size
        self.limit = limit


class XyRowLimitExceeded(AoiError):
    def __init__(self, path: Path, rows: int, limit: int):
        super().__init__(f"XY file has more than {limit:,} rows: {path}")
        self.path = path
        self.rows = rows
        self.limit = limit


class XyParseError(AoiError):
    def __init__(self, path: Path, bad_lines: tuple[tuple[int, str], ...]):
        super().__init__(f"{len(bad_lines)} unusable row(s) in XY file {path}")
        self.path = path
        self.bad_lines = bad_lines


class ProgramRootUnreachable(AoiError):
    def __init__(self, root: Path, cause: Exception):
        super().__init__(f"HLC program folder unreachable: {root} - {cause}")
        self.root = root
        self.cause = cause


class ProgramFileTooLarge(AoiError):
    def __init__(self, path: Path, size: int, limit: int):
        super().__init__(f"Program {path.name} is {size:,} bytes; the limit is {limit:,}")
        self.path = path
        self.size = size
        self.limit = limit


class ProgramDecodeError(AoiError):
    def __init__(self, path: Path, reason: str):
        super().__init__(f"Program {path.name} could not be decoded: {reason}")
        self.path = path
        self.reason = reason


class ProgramFormatUnsupported(AoiError):
    def __init__(self, path: Path, format_name: str):
        super().__init__(f"Program {path.name}: the {format_name} format is not decoded")
        self.path = path
        self.format_name = format_name


class FrameFitError(AoiError):
    def __init__(self, side_label: str, reason: str, matched: int = 0,
                 max_residual_mil: Optional[float] = None):
        super().__init__(f"{side_label}: program origin not usable - {reason}")
        self.side_label = side_label
        self.reason = reason
        self.matched = matched
        self.max_residual_mil = max_residual_mil


class AoiBuildError(AoiError):
    def __init__(self, side_label: str, problems: tuple[tuple[str, str], ...]):
        super().__init__(f"{side_label}: {len(problems)} row(s) cannot be written to the AOI file")
        self.side_label = side_label
        self.problems = problems


class AoiReadError(AoiError):
    def __init__(self, path: Path, reason: str):
        super().__init__(f"AOI file {path.name} could not be read: {reason}")
        self.path = path
        self.reason = reason


class BomUnavailable(AoiError):
    def __init__(self, reason: str):
        super().__init__(f"The audit's stored BOM is unavailable: {reason}")
        self.reason = reason


class AoiDestinationRejected(AoiError):
    def __init__(self, destination: Path, reason: str):
        super().__init__(f"Cannot write AOI files to {destination}: {reason}")
        self.destination = destination
        self.reason = reason


class AoiDestinationExists(AoiError):
    def __init__(self, existing: tuple[Path, ...]):
        names = ", ".join(p.name for p in existing)
        super().__init__(f"AOI files already exist: {names}")
        self.existing = existing


class AoiWriteError(AoiError):
    def __init__(self, destination: Path, cause: Exception,
                 written: tuple[Path, ...], replaced: tuple[Path, ...]):
        super().__init__(f"Writing AOI files to {destination} failed: {cause}")
        self.destination = destination
        self.cause = cause
        self.written = written
        self.replaced = replaced
