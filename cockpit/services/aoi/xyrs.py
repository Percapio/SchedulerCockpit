"""Customer XYRS export: on-demand location and parsing (Phase 52 section 3.1).

The file is found by its header, not its name. In the corpus, 946 files filed
as "XY" are BOM exports and 79 are drill files; only the header separates them.
"""

import csv
import io
import math
import os
import pathlib
import re

from cockpit.ingestion.errors import JobDirectoryNotFound, JobNumberMalformed, SourceRootUnreachable
from cockpit.ingestion.locator import JOB_NUMBER_GRAMMAR, assert_within_root, resolve_job_directory
from cockpit.ingestion.parsers.audit_bom import REFDES_TOKEN_REGEX

from .errors import XyFileTooLarge, XyParseError, XyRowLimitExceeded
from .types import (
    BoardSide, CadFiducial, CadPlacement, XyLimits, XyLocated, XyLocateOutcome, XyNotFound,
    XyPending, XyrsData,
)

XYRS_HEADER: tuple[str, ...] = ("RefDes", "PartNum", "XLocation", "YLocation", "Rotation", "Side")
_HEADER_PROBE_BYTES = 4096
_CANDIDATE_SUFFIXES = (".csv", ".txt")

# FIDTOP1, FIDBOT2, FID3: 49 corpus files carry fiducials this way, with a blank
# rotation. They are fiducials, not placements and not malformed rows.
_FIDUCIAL_DESIGNATOR = re.compile(r"^FID[A-Z_-]*\d*$", re.IGNORECASE)

_SIDES = {"T": BoardSide.TOP, "TOP": BoardSide.TOP, "B": BoardSide.BOTTOM,
          "BOT": BoardSide.BOTTOM, "BOTTOM": BoardSide.BOTTOM}


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _header_matches(fields: list[str]) -> bool:
    cleaned = [f.strip().strip('"').strip().lower() for f in fields[:len(XYRS_HEADER)]]
    return cleaned == [h.lower() for h in XYRS_HEADER]


def has_xyrs_header(path: pathlib.Path) -> bool:
    """True when the file's first line starts with the XYRS header columns.

    post: never raises; an unreadable file is simply not a candidate
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(_HEADER_PROBE_BYTES)
    except OSError:
        return False
    lines = _decode(head).splitlines()
    if not lines:
        return False
    return _header_matches(next(csv.reader([lines[0]]), []))


def _assert_root_reachable(source_root: pathlib.Path) -> None:
    try:
        if not source_root.is_dir():
            raise SourceRootUnreachable(source_root, FileNotFoundError("Root missing or not a directory"))
        with os.scandir(source_root) as entries:
            next(entries, None)
    except SourceRootUnreachable:
        raise
    except OSError as exc:
        raise SourceRootUnreachable(source_root, exc)


def locate_xy(job_number: str, source_root: pathlib.Path) -> XyLocateOutcome:
    """Finds the customer XYRS export for a job, on demand.

    pre:  source_root is the configured ingestion root
    post: XyLocated names one file directly in the job directory whose first
          line carries XYRS_HEADER; XyPending lists every candidate when several
          qualify; XyNotFound names the directory searched
    raises: JobNumberMalformed, SourceRootUnreachable, JobDirectoryNotFound,
            JobDirectoryEscapesRoot
    """
    job = (job_number or "").strip().upper()
    if not JOB_NUMBER_GRAMMAR.match(job):
        raise JobNumberMalformed(job_number)
    _assert_root_reachable(source_root)

    job_dir = resolve_job_directory(job, source_root)
    if not job_dir.is_dir():
        raise JobDirectoryNotFound(job, job_dir)
    assert_within_root(job_dir, source_root)

    candidates = sorted(
        (entry for entry in job_dir.iterdir()
         if entry.is_file() and entry.suffix.lower() in _CANDIDATE_SUFFIXES and has_xyrs_header(entry)),
        key=lambda p: p.name.lower(),
    )
    if not candidates:
        return XyNotFound(job_dir)
    if len(candidates) > 1:
        return XyPending(tuple(candidates))
    return XyLocated(candidates[0])


def _number(text: str) -> float | None:
    try:
        value = float(text.strip())
    except (ValueError, AttributeError):
        return None
    return value if math.isfinite(value) else None


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def parse_xyrs(path: pathlib.Path, limits: XyLimits | None = None) -> XyrsData:
    """Parses an XYRS export into placements and fiducial rows for both sides.

    pre:  path names a file whose header passed has_xyrs_header
    post: one CadPlacement per placement row and one CadFiducial per fiducial
          row; rows with an empty designator are skipped; designators in
          limits.excluded_designators are dropped before any other check
    raises: XyFileTooLarge, XyRowLimitExceeded, XyParseError (every bad row is
            listed, none is guessed), OSError
    """
    limits = limits or XyLimits()
    size = path.stat().st_size
    if size > limits.max_bytes:
        raise XyFileTooLarge(path, size, limits.max_bytes)

    rows = csv.reader(io.StringIO(_decode(path.read_bytes())))
    header = next(rows, None)
    if header is None or not _header_matches(header):
        raise XyParseError(path, ((1, "missing the RefDes,PartNum,XLocation,YLocation,Rotation,Side header"),))

    excluded = {d.upper() for d in limits.excluded_designators}
    placements: list[CadPlacement] = []
    fiducials: list[CadFiducial] = []
    bad: list[tuple[int, str]] = []
    seen: dict[tuple[str, BoardSide], int] = {}
    counted = 0

    for line_no, row in enumerate(rows, start=2):
        if not any(cell.strip() for cell in row):
            continue
        counted += 1
        if counted > limits.max_rows:
            raise XyRowLimitExceeded(path, counted, limits.max_rows)

        ref_des = _cell(row, 0)
        if not ref_des or ref_des.upper() in excluded:
            continue
        if not REFDES_TOKEN_REGEX.match(ref_des):
            bad.append((line_no, f"designator {ref_des!r} contains characters that are not allowed"))
            continue
        side = _SIDES.get(_cell(row, 5).upper())
        if side is None:
            bad.append((line_no, f"{ref_des}: side {_cell(row, 5)!r} is not T or B"))
            continue
        x, y = _number(_cell(row, 2)), _number(_cell(row, 3))
        if x is None or y is None:
            bad.append((line_no, f"{ref_des}: X/Y is not a number"))
            continue

        part_number = _cell(row, 1)
        if _FIDUCIAL_DESIGNATOR.match(ref_des) or part_number.upper() == "FIDUCIAL":
            fiducials.append(CadFiducial(ref_des, x, y, side, line_no))
            continue

        rotation = _number(_cell(row, 4))
        if rotation is None:
            bad.append((line_no, f"{ref_des}: rotation {_cell(row, 4)!r} is not a number"))
            continue
        key = (ref_des.upper(), side)
        if key in seen:
            bad.append((line_no, f"{ref_des}: also on line {seen[key]} for the same side"))
            continue
        seen[key] = line_no
        placements.append(CadPlacement(ref_des, part_number, x, y, rotation, side, line_no))

    if bad:
        raise XyParseError(path, tuple(bad))
    return XyrsData(path, tuple(placements), tuple(fiducials))
