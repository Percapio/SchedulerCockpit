"""Juki machine programs: location and the decoding the AOI export needs
(Phase 52 section 3.3).

Only what was confirmed on the corpus is decoded. A placement record starts with
the designator and a NUL; X and Y are little-endian int32 at +8 and +12 in
0.1 um, the angle an int16 at +16 in 1/80 degree. `.E42` records are 103 bytes
and `.E45` records 96, with the same field offsets. The placement block precedes
every other table that repeats designators, so the first occurrence of each
designator is its placement. `.E42` keeps its fiducials in a fixed table at
0xECA; `.E45` keeps them elsewhere, not yet located, and `.E46` is not decoded.
"""

import os
import pathlib
import re
import struct
from typing import Iterable

from cockpit.ingestion.errors import JobNumberMalformed
from cockpit.ingestion.locator import JOB_NUMBER_GRAMMAR, assert_within_root

from .errors import (
    ProgramDecodeError, ProgramFileTooLarge, ProgramFormatUnsupported, ProgramRootUnreachable,
)
from .types import (
    BoardSide, DecodedProgram, FiducialMark, MachinePlacement, ProgramArticlePending, ProgramFile,
    ProgramFolderNotFound, ProgramFormat, ProgramLimits, ProgramLocateOutcome, ProgramsLocated,
)

# <job>[<sep><article>]-<side>[<n>].E4x, e.g. B142286-T1.E45, B104573-3RD-B1.E45,
# B114233-2nd-T.e46. Superseded programs live in OLD\ subfolders, never read.
PROGRAM_NAME_RE = re.compile(
    r"^(?P<job>[A-Z]\d{6})(?:[ -](?P<article>\d+(?:ST|ND|RD|TH)))?"
    r"-(?P<side>[TB])(?P<split>\d*)\.(?P<ext>E4[256])$",
    re.IGNORECASE,
)

UNITS_PER_MIL = 254
_RECORD_FIELDS_END = 18
E42_MARK_TABLE_OFFSET = 0xECA
_MARK_RECORD = struct.Struct("<iiH")
FIDUCIAL_MARK_TAG = 6
_MAX_MARKS = 64


def _assert_root_reachable(program_root: pathlib.Path) -> None:
    try:
        if not program_root.is_dir():
            raise ProgramRootUnreachable(program_root, FileNotFoundError("Folder missing or not a directory"))
        with os.scandir(program_root) as entries:
            next(entries, None)
    except ProgramRootUnreachable:
        raise
    except OSError as exc:
        raise ProgramRootUnreachable(program_root, exc)


def _side(letter: str) -> BoardSide:
    return BoardSide.TOP if letter.upper() == "T" else BoardSide.BOTTOM


def _sort_key(program: ProgramFile) -> tuple:
    return (program.side is BoardSide.BOTTOM, program.split_index or 0, program.path.name.lower())


def locate_programs(job_number: str, program_root: pathlib.Path) -> ProgramLocateOutcome:
    """Lists the Juki program files for a job.

    pre:  program_root is the configured HLC program folder
    post: every returned path is a file directly inside <program_root>/<job> and
          resolves inside program_root; ProgramArticlePending when files for more
          than one article build are present
    raises: JobNumberMalformed, ProgramRootUnreachable, JobDirectoryEscapesRoot
    """
    job = (job_number or "").strip().upper()
    if not JOB_NUMBER_GRAMMAR.match(job):
        raise JobNumberMalformed(job_number)
    _assert_root_reachable(program_root)

    folder = program_root / job
    if not folder.is_dir():
        return ProgramFolderNotFound(folder)
    assert_within_root(folder, program_root)

    by_article: dict[str | None, list[ProgramFile]] = {}
    for entry in folder.iterdir():
        match = PROGRAM_NAME_RE.match(entry.name)
        if match is None or match.group("job").upper() != job or not entry.is_file():
            continue
        assert_within_root(entry, program_root)
        article = match.group("article").upper() if match.group("article") else None
        split = match.group("split")
        by_article.setdefault(article, []).append(ProgramFile(
            path=entry,
            side=_side(match.group("side")),
            split_index=int(split) if split else None,
            article=article,
            format=ProgramFormat(match.group("ext").upper()),
        ))

    grouped = {article: tuple(sorted(files, key=_sort_key)) for article, files in by_article.items()}
    if len(grouped) > 1:
        return ProgramArticlePending(folder, grouped)
    files = next(iter(grouped.values()), ())
    return ProgramsLocated(folder, files)


def _decode_e42_marks(path: pathlib.Path, data: bytes) -> tuple[FiducialMark, ...]:
    if len(data) < E42_MARK_TABLE_OFFSET + _MARK_RECORD.size:
        raise ProgramDecodeError(path, "file ends before the fiducial table")
    marks: list[FiducialMark] = []
    for index in range(_MAX_MARKS):
        offset = E42_MARK_TABLE_OFFSET + index * _MARK_RECORD.size
        if offset + _MARK_RECORD.size > len(data):
            break
        x, y, tag = _MARK_RECORD.unpack_from(data, offset)
        if tag != FIDUCIAL_MARK_TAG:
            break
        marks.append(FiducialMark(x, y))
    return tuple(marks)


def decode_program(
    program: ProgramFile,
    designators: Iterable[str],
    limits: ProgramLimits | None = None,
) -> DecodedProgram:
    """Decodes the placements and fiducial marks the AOI export needs.

    pre:  designators are the CAD designators of the program's side; each
          matches the BOM token grammar, so is ASCII
    post: placements keyed by designator, first occurrence only; marks is None
          for formats whose mark table is not known; every read is bounds-checked
    raises: ProgramFileTooLarge, ProgramFormatUnsupported, ProgramDecodeError,
            OSError
    """
    limits = limits or ProgramLimits()
    size = program.path.stat().st_size
    if size > limits.max_bytes:
        raise ProgramFileTooLarge(program.path, size, limits.max_bytes)
    if program.format is ProgramFormat.E46:
        raise ProgramFormatUnsupported(program.path, program.format.value)

    data = program.path.read_bytes()
    placements: dict[str, MachinePlacement] = {}
    names = sorted({d for d in designators if d}, key=len, reverse=True)
    if names:
        pattern = re.compile(
            rb"(?<![A-Za-z0-9])(" + b"|".join(re.escape(n.encode("ascii")) for n in names) + rb")\x00"
        )
        for match in pattern.finditer(data):
            name = match.group(1).decode("ascii")
            start = match.start()
            if name in placements or start + _RECORD_FIELDS_END > len(data):
                continue
            x, y = struct.unpack_from("<ii", data, start + 8)
            angle = struct.unpack_from("<h", data, start + 16)[0]
            placements[name] = MachinePlacement(name, x, y, angle)

    marks = _decode_e42_marks(program.path, data) if program.format is ProgramFormat.E42 else None
    return DecodedProgram(program, placements, marks)
