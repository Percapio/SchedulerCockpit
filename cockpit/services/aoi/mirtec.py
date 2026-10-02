"""The Mirtec MV-3 XY file: build, render and compare (Phase 52 sections 2, 4, 5).

Reproduces all 442 lines of the two sample jobs character for character.
"""

import math
import pathlib
import re
from typing import Optional, Sequence

from cockpit.utils.sorting import natural_sort_key

from .errors import AoiBuildError, AoiReadError
from .frame import side_label
from .types import (
    AoiComparison, AoiDifference, AoiFlag, AoiRow, AoiSideDocument, BoardSide, BomDesignatorMap,
    CadFiducial, CadPlacement, FiducialStatus, FlagKind, FrameStatus, ProgramFrame,
)

FIELD_COLUMNS: tuple[int, ...] = (0, 16, 40, 64, 88)
TAB_WIDTH = 8
MIRTEC_PART_NAME_LIMIT = 19
FIDUCIAL_PART_NAME = "FIDUCIAL"
LINE_END = "\r\n"
_MAX_AOI_FILE_BYTES = 8 * 1024 * 1024
_NUMERIC_NAME = re.compile(r"^\d+(?:\.\d+)?$")


def align_fields(fields: Sequence[str]) -> str:
    """Joins fields so each starts on its Mirtec column using 8-column tab stops.

    pre:  len(fields) == len(FIELD_COLUMNS); no field contains TAB, CR or LF
    post: field i starts at FIELD_COLUMNS[i] unless the previous field overruns
          it, in which case it starts at the next tab stop after one tab
    """
    if len(fields) != len(FIELD_COLUMNS):
        raise ValueError(f"expected {len(FIELD_COLUMNS)} fields, got {len(fields)}")
    line, column = "", 0
    for index, value in enumerate(fields):
        line += value
        column += len(value)
        if index < len(fields) - 1:
            line += "\t"
            column = (column // TAB_WIDTH + 1) * TAB_WIDTH
            while column < FIELD_COLUMNS[index + 1]:
                line += "\t"
                column += TAB_WIDTH
    return line


def aoi_part_name(bom_part_number: str) -> str:
    """Maps a BOM part number to the Mirtec part-name field.

    post: "DNI" passes through; otherwise commas become hyphens, whitespace is
          removed and the result is cut to MIRTEC_PART_NAME_LIMIT characters
    raises: ValueError when the result is empty or not printable ASCII
    """
    if bom_part_number.strip().upper() == "DNI":
        return "DNI"
    normalised = re.sub(r"\s+", "", bom_part_number.replace(",", "-"))
    if not normalised or not all(33 <= ord(ch) <= 126 for ch in normalised):
        raise ValueError(f"part name {bom_part_number!r} is empty or not printable ASCII")
    return normalised[:MIRTEC_PART_NAME_LIMIT]


def format_mil(value: float) -> str:
    """%.6f, with negative zero written as 0.000000."""
    text = f"{value:.6f}"
    return "0.000000" if text == "-0.000000" else text


def excel_sort_key(part_name: str) -> tuple:
    """Excel's ascending order: numbers before text, text case-insensitive."""
    if _NUMERIC_NAME.match(part_name):
        return (0, float(part_name), "")
    return (1, 0.0, part_name.upper())


def build_aoi_side(
    side: BoardSide,
    cad: Sequence[CadPlacement],
    cad_fiducials: Sequence[CadFiducial],
    names: BomDesignatorMap,
    frame: Optional[ProgramFrame],
    marks: Optional[Sequence[tuple[float, float]]],
    extra_flags: Sequence[AoiFlag] = (),
) -> AoiSideDocument:
    """Builds one side's AOI document; fiducials are numbered only at render time.

    pre:  cad and cad_fiducials hold this side's rows only; marks are already in
          the AOI frame and are only supplied together with frame
    post: rows in Excel part-name order, stable on XY order; frame_status is
          PROGRAM_ORIGIN iff frame is present; fiducials from marks, else CAD
          fiducial rows (translated by the frame when present), else none
    raises: AoiBuildError listing every row that cannot be written
    """
    offset_x, offset_y = (frame.offset_x_mil, frame.offset_y_mil) if frame else (0.0, 0.0)
    problems: list[tuple[str, str]] = []
    missing: list[str] = []
    keyed: list[tuple[tuple, int, AoiRow]] = []

    for order, placement in enumerate(cad):
        if not float(placement.rotation_deg).is_integer():
            problems.append((placement.ref_des, f"rotation {placement.rotation_deg:g} is not a whole number"))
            continue
        source = names.names.get(placement.ref_des)
        if source is None:
            missing.append(placement.ref_des)
            source = placement.part_number
        try:
            part_name = aoi_part_name(source)
        except ValueError as exc:
            problems.append((placement.ref_des, str(exc)))
            continue
        row = AoiRow(
            ref_des=placement.ref_des,
            part_name=part_name,
            x_mil=placement.x_mil + offset_x,
            y_mil=placement.y_mil + offset_y,
            angle_deg=int(placement.rotation_deg),
        )
        keyed.append((excel_sort_key(part_name), order, row))

    if problems:
        raise AoiBuildError(side_label(side), tuple(problems))
    keyed.sort(key=lambda item: (item[0], item[1]))

    if marks:
        fiducials = tuple(marks)
        fiducial_status = FiducialStatus.PROGRAM_MARKS
    elif cad_fiducials:
        ordered = sorted(cad_fiducials, key=lambda f: natural_sort_key(f.ref_des))
        fiducials = tuple((round(f.x_mil + offset_x, 2), round(f.y_mil + offset_y, 2)) for f in ordered)
        fiducial_status = FiducialStatus.CAD_ROWS
    else:
        fiducials = ()
        fiducial_status = FiducialStatus.NONE

    side_refs = {p.ref_des for p in cad}
    flags = list(extra_flags)
    for conflict in names.conflicts:
        if conflict.ref_des in side_refs:
            flags.append(AoiFlag(
                FlagKind.BOM_CONFLICT,
                f"{conflict.ref_des} is on several BOM lines ({', '.join(conflict.part_numbers)}); "
                f"used {conflict.chosen}",
                (conflict.ref_des,),
            ))
    if missing:
        flags.append(AoiFlag(
            FlagKind.MISSING_FROM_BOM,
            f"{len(missing)} designator(s) not on the Audit BOM; the XY part number was used",
            tuple(missing),
        ))
    if frame is not None and frame.outliers:
        flags.append(AoiFlag(
            FlagKind.FRAME_OUTLIERS,
            f"{len(frame.outliers)} placement(s) sit elsewhere in the program than in the XY file",
            frame.outliers,
        ))

    return AoiSideDocument(
        side=side,
        rows=tuple(row for _key, _order, row in keyed),
        fiducials=fiducials,
        frame_status=FrameStatus.PROGRAM_ORIGIN if frame is not None else FrameStatus.CAD_ORIGIN,
        fiducial_status=fiducial_status,
        flags=tuple(flags),
        frame=frame,
    )


def _fiducial_rows(document: AoiSideDocument, first_fiducial_number: int) -> list[AoiRow]:
    return [
        AoiRow(f"FID{first_fiducial_number + i}", FIDUCIAL_PART_NAME, x, y, 0)
        for i, (x, y) in enumerate(document.fiducials)
    ]


def render_mirtec(document: AoiSideDocument, first_fiducial_number: int = 1) -> str:
    """Renders a document in the exact Mirtec text form.

    pre:  first_fiducial_number >= 1
    post: ASCII, CRLF after every line, each line align_fields of the five
          fields; fiducials named FID<first_fiducial_number>, ...
    """
    if first_fiducial_number < 1:
        raise ValueError("fiducial numbering starts at 1 or above")
    lines = [
        align_fields([row.ref_des, row.part_name, format_mil(row.x_mil), format_mil(row.y_mil),
                      str(row.angle_deg)])
        for row in list(document.rows) + _fiducial_rows(document, first_fiducial_number)
    ]
    text = "".join(line + LINE_END for line in lines)
    text.encode("ascii")
    return text


def output_file_name(side: BoardSide, job_number: str) -> str:
    return f"{side.value}-{job_number}-XY.txt"


def parse_mirtec(path: pathlib.Path) -> list[AoiRow]:
    """Reads a Mirtec XY file back into rows.

    raises: AoiReadError, OSError
    """
    size = path.stat().st_size
    if size > _MAX_AOI_FILE_BYTES:
        raise AoiReadError(path, f"{size:,} bytes is larger than any AOI XY file")
    rows: list[AoiRow] = []
    for line_no, line in enumerate(path.read_bytes().decode("ascii", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        fields = [f for f in line.split("\t") if f != ""]
        if len(fields) != len(FIELD_COLUMNS):
            raise AoiReadError(path, f"line {line_no} has {len(fields)} fields, expected {len(FIELD_COLUMNS)}")
        try:
            x, y, angle = float(fields[2]), float(fields[3]), float(fields[4])
        except ValueError:
            raise AoiReadError(path, f"line {line_no} has a non-numeric X, Y or angle")
        if not all(math.isfinite(v) for v in (x, y, angle)):
            raise AoiReadError(path, f"line {line_no} has a non-finite number")
        rows.append(AoiRow(fields[0], fields[1], x, y, int(angle) if angle.is_integer() else angle))
    return rows


def compare_aoi(
    expected: AoiSideDocument,
    first_fiducial_number: int,
    existing: pathlib.Path,
    tolerance_mil: float = 0.5,
) -> AoiComparison:
    """Compares an existing AOI file with the document Cockpit would generate.

    post: designators missing on either side, part-name differences, X/Y deltas
          above tolerance_mil and angle differences; fiducials compared by name
    raises: AoiReadError, OSError
    """
    wanted = {r.ref_des: r for r in list(expected.rows) + _fiducial_rows(expected, first_fiducial_number)}
    found = {r.ref_des: r for r in parse_mirtec(existing)}
    differences: list[AoiDifference] = []
    for ref_des in sorted(set(wanted) & set(found), key=natural_sort_key):
        want, have = wanted[ref_des], found[ref_des]
        if want.part_name != have.part_name:
            differences.append(AoiDifference(ref_des, "part name", want.part_name, have.part_name))
        if math.hypot(want.x_mil - have.x_mil, want.y_mil - have.y_mil) > tolerance_mil:
            differences.append(AoiDifference(
                ref_des, "position",
                f"{format_mil(want.x_mil)}, {format_mil(want.y_mil)}",
                f"{format_mil(have.x_mil)}, {format_mil(have.y_mil)}",
            ))
        if want.angle_deg != have.angle_deg:
            differences.append(AoiDifference(ref_des, "angle", str(want.angle_deg), str(have.angle_deg)))
    return AoiComparison(
        side=expected.side,
        existing=existing,
        missing_in_existing=tuple(sorted(set(wanted) - set(found), key=natural_sort_key)),
        extra_in_existing=tuple(sorted(set(found) - set(wanted), key=natural_sort_key)),
        differences=tuple(differences),
    )
