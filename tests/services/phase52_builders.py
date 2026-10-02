"""Synthetic Phase 52 fixtures. No customer data: every name and coordinate is invented.

Program bytes follow the layout confirmed on the corpus: a placement record is
the designator + NUL, int32 X/Y at +8/+12 (0.1 um), int16 angle at +16; `.E42`
records are 103 bytes apart and `.E45` 96; `.E42` fiducials sit at 0xECA as
(int32 X, int32 Y, uint16 tag 6).
"""

import csv
import io
import pathlib
import struct
from dataclasses import dataclass

import openpyxl

E42_STRIDE = 103
E45_STRIDE = 96
TABLE_OFFSET = 0x1000
UNITS_PER_MIL = 254

JOB = "B123456"
OFFSET = (-1000.0, 250.0)

# (ref, CAD part number, x mil, y mil, rotation, side)
TOP_ROWS = [
    ("R1", "RC0402FR-0710KL", 1000, 2000, 0, "T"),
    ("R2", "RC0402FR-0710KL", 1100, 2000, 90, "T"),
    ("C1", "GRM155R71C104KA88D", 1200, 2100, 180, "T"),
    ("C2", "GRM155R71C104KA88D", 1300, 2100, 270, "T"),
    ("U1", "STM32F091RCT6", 1500, 2500, 0, "T"),
    ("TP1", "5019", 1600, 2600, 0, "T"),
    ("Y1", "Q27,120-JXS11P4-12-10/30", 1700, 2700, 270, "T"),
    ("Q9", "TSC497CX RFG", 1800, 2800, 0, "T"),
    ("R5", "NA", 1900, 2900, 0, "T"),
]
BOTTOM_ROWS = [
    ("R10", "ERA3AEB4532V", 500, 600, 180, "B"),
    ("R11", "ERA3AEB4532V", 520, 600, 180, "B"),
    ("R12", "ERA3AEB4532V", 540, 600, 0, "B"),
    ("R13", "ERA3AEB4532V", 560, 600, 0, "B"),
    ("R14", "ERA3AEB4532V", 580, 600, 90, "B"),
]
CAD_FIDUCIAL_LINES = [
    "FIDTOP1, ,100,100, ,T, ",
    "FIDTOP2, ,2900,3100, ,T, ",
    "FIDBOT1, ,150,120, ,B, ",
]
TOP_MARKS_AOI = [(-500.0, 3000.0), (900.0, 3000.0), (-500.0, -100.0)]
BOTTOM_MARKS_AOI = [(-400.0, 900.0), (300.0, 900.0)]

# (PartNum, Ref_Des) in sheet order
BOM_LINES = [
    ("RC0402FR-0710KL", "R1, R2"),
    ("GRM155R71C104KA88D", "C1, C2"),
    ("STM32F091RCT6", "U1   *please x-ray*"),
    (5019, "TP1"),
    ("Q27,120-JXS11P4-12-10/30-T2-FU-AEC-LF", "Y1"),
    ("TSC497CX RFG", "Q9"),
    ("ERA-3AEB4532V", "R10, R11, R12, R13, R14"),
    ("DNI", "R5"),
]

BOM_HEADERS = [
    "Find#", "PartNum", "Count", "Ref_Des", "Package", "Description", "SMT/THT",
    "Qty Need", "Qty On hand", "Qty short", "comment",
]


def units(mil: float) -> int:
    return round(mil * UNITS_PER_MIL)


def program_bytes(placements, marks=(), stride=E42_STRIDE, mark_table=True, trailing_refs=()) -> bytes:
    """placements: (ref, x_units, y_units, angle_units); marks: (x_units, y_units)."""
    trailing_start = TABLE_OFFSET + stride * (len(placements) + 1)
    size = trailing_start + 12 * len(trailing_refs) + 64
    data = bytearray(size)
    data[0:14] = b"SYNTH,ADVANCED"
    if mark_table:
        for index, (x, y) in enumerate(marks):
            struct.pack_into("<iiH", data, 0xECA + 10 * index, x, y, 6)
        struct.pack_into("<iiH", data, 0xECA + 10 * len(marks), 818, 0, 0)
    for index, (ref, x, y, angle) in enumerate(placements):
        offset = TABLE_OFFSET + index * stride
        name = ref.encode("ascii")
        data[offset:offset + len(name)] = name
        struct.pack_into("<h", data, offset + 6, 2)
        struct.pack_into("<iih", data, offset + 8, x, y, angle)
    for index, ref in enumerate(trailing_refs):
        offset = trailing_start + 12 * index
        name = ref.encode("ascii")
        data[offset:offset + len(name)] = name
    return bytes(data)


def placements_for(rows, offset=OFFSET, mirrored=False, skip=("R5",)):
    sign = -1 if mirrored else 1
    return [
        (ref, units(sign * (x + offset[0])), units(y + offset[1]), int(rot) * 80)
        for ref, _part, x, y, rot, _side in rows if ref not in skip
    ]


def marks_for(points_aoi, mirrored=False):
    sign = -1 if mirrored else 1
    return [(units(sign * x), units(y)) for x, y in points_aoi]


def write_xyrs(path: pathlib.Path, rows, extra_lines=(), header=True) -> pathlib.Path:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if header:
        writer.writerow(["RefDes", "PartNum", "XLocation", "YLocation", "Rotation", "Side", "Desc"])
    for ref, part, x, y, rot, side in rows:
        writer.writerow([ref, part, x, y, rot, side, "desc"])
    text = buffer.getvalue() + "".join(line + "\n" for line in extra_lines)
    path.write_text(text, encoding="utf-8")
    return path


def write_bom(path: pathlib.Path, lines=BOM_LINES) -> pathlib.Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "AUDIT BOM"
    sheet.append(BOM_HEADERS)
    for index, (part_number, refs) in enumerate(lines, 1):
        sheet.append([index, part_number, 1, refs, "402", "desc", "SMT", 1, 1, 0, ""])
    workbook.save(path)
    return path


@dataclass
class SyntheticJob:
    job: str
    source_root: pathlib.Path
    program_root: pathlib.Path
    job_dir: pathlib.Path
    program_dir: pathlib.Path
    xy_path: pathlib.Path
    stored_bom: pathlib.Path


def build_job(tmp_path: pathlib.Path, *, programs: str = "both", cad_fiducials: bool = True,
              article_copy: bool = False) -> SyntheticJob:
    """programs: "both", "top", "none" (folder absent), "empty" (folder, no files)."""
    source_root = tmp_path / "share"
    job_dir = source_root / "B123xxx" / "B1234xx" / f"{JOB} Example Customer"
    job_dir.mkdir(parents=True)
    extra = list(CAD_FIDUCIAL_LINES) if cad_fiducials else []
    extra.append("PCB,B123456-PCB,0,0,0,T,board")
    xy_path = write_xyrs(job_dir / f"{JOB}_XYRS_Export.csv", TOP_ROWS + BOTTOM_ROWS, extra)
    (job_dir / f"{JOB}.csv").write_text(
        "Find#,Count,Ref_Des,PartNum,Value,PCB_Decal,Package,Description,SMT/THT\n1,1,R1,X,1K,U,0402,d,SMT\n",
        encoding="utf-8",
    )

    storage = tmp_path / "storage"
    storage.mkdir()
    stored_bom = write_bom(storage / f"{JOB} AUDIT BOM.xlsx")

    program_root = tmp_path / "hlc"
    program_dir = program_root / JOB
    program_root.mkdir()
    if programs != "none":
        program_dir.mkdir()
    if programs in ("both", "top"):
        (program_dir / f"{JOB}-T.E42").write_bytes(
            program_bytes(placements_for(TOP_ROWS), marks_for(TOP_MARKS_AOI)))
        old = program_dir / "OLD"
        old.mkdir()
        (old / f"{JOB}-T.E42").write_bytes(program_bytes([("R1", 1, 1, 0)]))
    if programs == "both":
        (program_dir / f"{JOB}-B.E42").write_bytes(
            program_bytes(placements_for(BOTTOM_ROWS, mirrored=True), marks_for(BOTTOM_MARKS_AOI, mirrored=True)))
    if article_copy:
        (program_dir / f"{JOB}-2ND-T.E42").write_bytes(
            program_bytes(placements_for(TOP_ROWS, offset=(-2000.0, 0.0)), marks_for(TOP_MARKS_AOI)))
    return SyntheticJob(JOB, source_root, program_root, job_dir, program_dir, xy_path, stored_bom)
