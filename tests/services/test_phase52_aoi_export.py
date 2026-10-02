"""Phase 52 — AOI XY export (Mirtec MV-3), on synthetic fixtures only."""

import pathlib

import pytest

from cockpit.ingestion.errors import JobNumberMalformed
from cockpit.services.aoi import export as export_module
from cockpit.services.aoi.bom_designators import read_bom_designators, resolve_designator
from cockpit.services.aoi.errors import (
    AoiBuildError, AoiDestinationExists, AoiDestinationRejected, AoiReadError, AoiWriteError,
    FrameFitError, ProgramDecodeError, ProgramFileTooLarge, ProgramFormatUnsupported,
    ProgramRootUnreachable, XyFileTooLarge, XyParseError, XyRowLimitExceeded,
)
from cockpit.services.aoi.export import (
    AoiExportPlan, AoiExportRequest, planned_outputs, prepare_aoi_export, write_aoi_files,
)
from cockpit.services.aoi.frame import derive_frame
from cockpit.services.aoi.juki_program import decode_program, locate_programs
from cockpit.services.aoi.mirtec import (
    aoi_part_name, align_fields, build_aoi_side, compare_aoi, excel_sort_key, format_mil,
    parse_mirtec, render_mirtec,
)
from cockpit.services.aoi.types import (
    BoardSide, BomDesignatorMap, CadPlacement, ConflictResolution, DecodedProgram, FiducialStatus,
    FlagKind, FrameStatus, FrameTolerance, MachinePlacement, ProgramArticlePending, ProgramFile,
    ProgramFolderNotFound, ProgramFormat, ProgramLimits, ProgramsLocated, XyLimits, XyLocated,
    XyNotFound, XyPending,
)
from cockpit.services.aoi.xyrs import locate_xy, parse_xyrs
from cockpit.ui.error_messages import render

from tests.services.phase52_builders import (
    BOM_LINES, BOTTOM_ROWS, E45_STRIDE, JOB, TOP_ROWS, build_job, marks_for, placements_for,
    program_bytes, units, write_bom, write_xyrs,
)


def request_for(job, **overrides) -> AoiExportRequest:
    values = dict(job_number=job.job, source_root=job.source_root, stored_bom=job.stored_bom,
                  program_root=job.program_root)
    values.update(overrides)
    return AoiExportRequest(**values)


def plan_for(job, **overrides) -> AoiExportPlan:
    plan = prepare_aoi_export(request_for(job, **overrides))
    assert isinstance(plan, AoiExportPlan), plan
    return plan


# --- section 2: the format ------------------------------------------------------

def test_align_fields_pads_to_the_mirtec_columns():
    assert align_fields(["TP7", "5019", "-1234.000000", "456.000000", "180"]) == \
        "TP7\t\t5019\t\t\t-1234.000000\t\t456.000000\t\t180"


def test_align_fields_uses_one_tab_after_a_19_character_part_name():
    assert align_fields(["C9", "ABCDEFGHIJKLMNOPQRS", "-1.000000", "2.000000", "0"]) == \
        "C9\t\tABCDEFGHIJKLMNOPQRS\t-1.000000\t\t2.000000\t\t0"


def test_align_fields_handles_an_eight_character_designator():
    assert align_fields(["ABCDEFGH", "X", "0.000000", "0.000000", "0"]) == \
        "ABCDEFGH\tX\t\t\t0.000000\t\t0.000000\t\t0"


def test_align_fields_moves_to_the_next_tab_stop_after_an_overrun():
    assert align_fields(["R1", "ABCDEFGHIJKLMNOPQRSTUVWX", "1.000000", "2.000000", "0"]) == \
        "R1\t\tABCDEFGHIJKLMNOPQRSTUVWX\t1.000000\t2.000000\t\t0"


@pytest.mark.parametrize("raw, expected", [
    ("DNI", "DNI"),
    (" dni ", "DNI"),
    ("Q27,120-JXS11P4-12-10/30-T2-FU-AEC-LF", "Q27-120-JXS11P4-12-"),
    ("TSC497CX RFG", "TSC497CXRFG"),
    ("ABM11AIG-20.000MHZ-8-1Z-T", "ABM11AIG-20.000MHZ-"),
    ("5019", "5019"),
])
def test_aoi_part_name(raw, expected):
    assert aoi_part_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "Résistor"])
def test_aoi_part_name_rejects_empty_and_non_ascii(raw):
    with pytest.raises(ValueError):
        aoi_part_name(raw)


def test_negative_zero_is_written_as_plain_zero():
    assert format_mil(-0.0) == "0.000000"
    assert format_mil(-0.0000001) == "0.000000"
    assert format_mil(-1505.0) == "-1505.000000"


def test_excel_order_puts_numbers_first_then_text_case_insensitive():
    names = ["TSC497CXRFG", "04023C104KAT2A", "5019", "dni", "100", "GRM155"]
    assert sorted(names, key=excel_sort_key) == ["100", "5019", "04023C104KAT2A", "dni", "GRM155", "TSC497CXRFG"]


# --- section 3.1: customer XY --------------------------------------------------

def test_locate_xy_finds_the_xyrs_export_by_header(tmp_path):
    job = build_job(tmp_path)
    assert locate_xy(JOB, job.source_root) == XyLocated(job.xy_path)


def test_locate_xy_reports_several_candidates(tmp_path):
    job = build_job(tmp_path)
    second = write_xyrs(job.job_dir / "older_XYRS.txt", TOP_ROWS[:2])
    outcome = locate_xy(JOB, job.source_root)
    assert isinstance(outcome, XyPending)
    assert set(outcome.candidates) == {job.xy_path, second}


def test_locate_xy_reports_nothing_found(tmp_path):
    job = build_job(tmp_path)
    job.xy_path.unlink()
    assert locate_xy(JOB, job.source_root) == XyNotFound(job.job_dir)


def test_locate_xy_rejects_a_malformed_job_number(tmp_path):
    job = build_job(tmp_path)
    with pytest.raises(JobNumberMalformed):
        locate_xy("123456", job.source_root)


def test_parse_xyrs_splits_placements_fiducials_and_drops_pcb(tmp_path):
    job = build_job(tmp_path)
    data = parse_xyrs(job.xy_path)
    refs = [p.ref_des for p in data.placements]
    assert refs == [r[0] for r in TOP_ROWS + BOTTOM_ROWS]
    assert "PCB" not in refs
    assert [(f.ref_des, f.side) for f in data.fiducials] == [
        ("FIDTOP1", BoardSide.TOP), ("FIDTOP2", BoardSide.TOP), ("FIDBOT1", BoardSide.BOTTOM)]
    y1 = next(p for p in data.placements if p.ref_des == "Y1")
    assert y1.part_number == "Q27,120-JXS11P4-12-10/30"


def test_parse_xyrs_lists_every_bad_row(tmp_path):
    path = write_xyrs(tmp_path / "x.csv", [("R1", "P", 1, 2, 0, "T")], extra_lines=[
        "R2,P,abc,2,0,T,d",
        "R3,P,1,2,0,X,d",
        "R4,P,1,2,,T,d",
        "R1,P,5,5,0,T,d",
    ])
    with pytest.raises(XyParseError) as caught:
        parse_xyrs(path)
    assert [line for line, _reason in caught.value.bad_lines] == [3, 4, 5, 6]


def test_parse_xyrs_requires_the_header(tmp_path):
    path = write_xyrs(tmp_path / "x.csv", [("R1", "P", 1, 2, 0, "T")], header=False)
    with pytest.raises(XyParseError):
        parse_xyrs(path)


def test_parse_xyrs_enforces_the_size_and_row_limits(tmp_path):
    path = write_xyrs(tmp_path / "x.csv", [(f"R{i}", "P", i, i, 0, "T") for i in range(1, 30)])
    with pytest.raises(XyFileTooLarge):
        parse_xyrs(path, XyLimits(max_bytes=100))
    with pytest.raises(XyRowLimitExceeded):
        parse_xyrs(path, XyLimits(max_rows=10))


# --- section 3.3: programs -------------------------------------------------------

def test_locate_programs_reads_the_job_folder_only(tmp_path):
    job = build_job(tmp_path)
    outcome = locate_programs(JOB, job.program_root)
    assert isinstance(outcome, ProgramsLocated)
    assert [(p.path.name, p.side) for p in outcome.files] == [
        (f"{JOB}-T.E42", BoardSide.TOP), (f"{JOB}-B.E42", BoardSide.BOTTOM)]
    assert all("OLD" not in p.path.parts for p in outcome.files)


def test_locate_programs_reads_split_sides_and_formats(tmp_path):
    root = tmp_path / "hlc"
    folder = root / JOB
    folder.mkdir(parents=True)
    for name in (f"{JOB}-T1.E45", f"{JOB}-T2.E42", f"{JOB}-b1.e46", "notes.txt", f"{JOB} FD.txt"):
        (folder / name).write_bytes(b"x")
    outcome = locate_programs(JOB, root)
    assert [(p.side, p.split_index, p.format) for p in outcome.files] == [
        (BoardSide.TOP, 1, ProgramFormat.E45), (BoardSide.TOP, 2, ProgramFormat.E42),
        (BoardSide.BOTTOM, 1, ProgramFormat.E46)]


def test_locate_programs_asks_which_article(tmp_path):
    job = build_job(tmp_path, article_copy=True)
    outcome = locate_programs(JOB, job.program_root)
    assert isinstance(outcome, ProgramArticlePending)
    assert outcome.articles() == [None, "2ND"]
    assert [p.path.name for p in outcome.choose("2ND").files] == [f"{JOB}-2ND-T.E42"]


def test_locate_programs_without_a_job_folder(tmp_path):
    job = build_job(tmp_path, programs="none")
    assert locate_programs(JOB, job.program_root) == ProgramFolderNotFound(job.program_dir)


def test_locate_programs_reports_an_unreachable_root(tmp_path):
    with pytest.raises(ProgramRootUnreachable):
        locate_programs(JOB, tmp_path / "missing")


def _program(path, fmt=ProgramFormat.E42, side=BoardSide.TOP):
    return ProgramFile(path=path, side=side, split_index=None, article=None, format=fmt)


def test_decode_e42_reads_placements_and_marks(tmp_path):
    path = tmp_path / f"{JOB}-T.E42"
    path.write_bytes(program_bytes([("R1", 100, -200, 7200), ("C10", 5, 6, 0)], [(11, 22), (33, 44)]))
    decoded = decode_program(_program(path), ["R1", "C10", "R2"])
    assert decoded.placements["R1"] == MachinePlacement("R1", 100, -200, 7200)
    assert set(decoded.placements) == {"R1", "C10"}
    assert [(m.x_units, m.y_units) for m in decoded.marks] == [(11, 22), (33, 44)]


def test_decode_keeps_the_first_occurrence_of_each_designator(tmp_path):
    path = tmp_path / f"{JOB}-T1.E45"
    path.write_bytes(program_bytes([("R1", 1, 2, 0)], stride=E45_STRIDE, mark_table=False,
                                   trailing_refs=["R1"]))
    decoded = decode_program(_program(path, ProgramFormat.E45), ["R1"])
    assert decoded.placements["R1"].x_units == 1
    assert decoded.marks is None


def test_decode_does_not_match_a_designator_inside_a_longer_one(tmp_path):
    path = tmp_path / f"{JOB}-T.E42"
    path.write_bytes(program_bytes([("R10", 9, 9, 0), ("R1", 1, 1, 0)]))
    decoded = decode_program(_program(path), ["R1", "R10"])
    assert decoded.placements["R1"].x_units == 1
    assert decoded.placements["R10"].x_units == 9


def test_decode_refuses_e46_large_and_truncated_files(tmp_path):
    path = tmp_path / "p.E46"
    path.write_bytes(b"\0" * 64)
    with pytest.raises(ProgramFormatUnsupported):
        decode_program(_program(path, ProgramFormat.E46), ["R1"])
    big = tmp_path / "big.E42"
    big.write_bytes(program_bytes([("R1", 1, 1, 0)]))
    with pytest.raises(ProgramFileTooLarge):
        decode_program(_program(big), ["R1"], ProgramLimits(max_bytes=100))
    short = tmp_path / "short.E42"
    short.write_bytes(b"R1\0" + b"\0" * 40)
    with pytest.raises(ProgramDecodeError):
        decode_program(_program(short), ["R1"])


# --- section 3.4: the frame ---------------------------------------------------------

def _cad(rows):
    side = {"T": BoardSide.TOP, "B": BoardSide.BOTTOM}
    return [CadPlacement(r, p, float(x), float(y), float(rot), side[s], i)
            for i, (r, p, x, y, rot, s) in enumerate(rows, 2)]


def _decoded(placements, side=BoardSide.TOP, name="p.E42", marks=()):
    return DecodedProgram(
        _program(pathlib.Path(name), side=side),
        {ref: MachinePlacement(ref, x, y, a) for ref, x, y, a in placements},
        tuple(marks),
    )


def test_frame_identity_recovers_the_origin():
    frame = derive_frame(BoardSide.TOP, _cad(TOP_ROWS), [_decoded(placements_for(TOP_ROWS))])
    assert (frame.offset_x_mil, frame.offset_y_mil, frame.mirrored_x) == (-1000.0, 250.0, False)
    assert frame.matched == 8 and frame.outliers == ()


def test_frame_detects_a_mirrored_bottom_program():
    frame = derive_frame(BoardSide.BOTTOM, _cad(BOTTOM_ROWS),
                         [_decoded(placements_for(BOTTOM_ROWS, mirrored=True), BoardSide.BOTTOM)])
    assert (frame.offset_x_mil, frame.offset_y_mil, frame.mirrored_x) == (-1000.0, 250.0, True)


def test_frame_rounds_away_one_unit_storage_error():
    placements = [(r, x + 1, y - 1, a) for r, x, y, a in placements_for(TOP_ROWS)]
    frame = derive_frame(BoardSide.TOP, _cad(TOP_ROWS), [_decoded(placements)])
    assert (frame.offset_x_mil, frame.offset_y_mil) == (-1000.0, 250.0)


def test_frame_needs_enough_matches():
    with pytest.raises(FrameFitError):
        derive_frame(BoardSide.TOP, _cad(TOP_ROWS), [_decoded(placements_for(TOP_ROWS)[:4])])


def test_frame_reports_outliers_but_keeps_the_origin():
    rows = TOP_ROWS + [(f"R{i}", "P", 100 * i, 50, 0, "T") for i in range(20, 32)]
    placements = placements_for(rows)
    placements[0] = (placements[0][0], placements[0][1] + units(40), placements[0][2], 0)
    frame = derive_frame(BoardSide.TOP, _cad(rows), [_decoded(placements)])
    assert frame.outliers == ("R1",)
    assert (frame.offset_x_mil, frame.offset_y_mil) == (-1000.0, 250.0)


def test_frame_rejects_a_program_that_does_not_line_up():
    rotated = [(r, y, x, a) for r, x, y, a in placements_for(TOP_ROWS)]
    with pytest.raises(FrameFitError):
        derive_frame(BoardSide.TOP, _cad(TOP_ROWS), [_decoded(rotated)])


def test_split_programs_must_share_an_origin():
    first = _decoded(placements_for(TOP_ROWS), name="T1.E45")
    second = _decoded(placements_for(TOP_ROWS, offset=(-900.0, 250.0)), name="T2.E42")
    with pytest.raises(FrameFitError):
        derive_frame(BoardSide.TOP, _cad(TOP_ROWS), [first, second])


def test_split_programs_with_one_origin_combine():
    first = _decoded(placements_for(TOP_ROWS[:5]), name="T1.E45")
    second = _decoded(placements_for(TOP_ROWS[3:]), name="T2.E42")
    tolerance = FrameTolerance(min_matches=3)
    frame = derive_frame(BoardSide.TOP, _cad(TOP_ROWS), [first, second], tolerance)
    assert frame.offset_x_mil == -1000.0 and frame.matched == 10


# --- section 3.2: the BOM ----------------------------------------------------------------

def test_dni_line_wins_over_a_part_line():
    chosen, conflict = resolve_designator("FB1", ["744771002", "DNI"], "744771002")
    assert chosen == "DNI" and conflict.resolution is ConflictResolution.DNI_LINE


def test_cad_matching_line_wins_when_two_parts_share_a_designator():
    chosen, conflict = resolve_designator("XF1", ["3568", "0997005.WXN"], "0997005WXN")
    assert chosen == "0997005.WXN" and conflict.resolution is ConflictResolution.CAD_MATCH


def test_truncated_cad_part_number_still_matches():
    chosen, conflict = resolve_designator("Y1", ["OTHER-PART", "ABM11AIG-20.000MHZ-8-1Z-T"],
                                          "ABM11AIG-20.000MHZ-8-1Z-")
    assert chosen == "ABM11AIG-20.000MHZ-8-1Z-T" and conflict.resolution is ConflictResolution.CAD_MATCH


def test_first_line_wins_otherwise():
    chosen, conflict = resolve_designator("J1", ["CONN-A", "CONN-B"], "UNRELATED")
    assert chosen == "CONN-A" and conflict.resolution is ConflictResolution.FIRST_LINE


def test_the_same_part_on_two_lines_is_not_a_conflict():
    chosen, conflict = resolve_designator("R1", ["RC0402FR-0710KL", "rc0402fr 0710kl"], None)
    assert chosen == "RC0402FR-0710KL" and conflict is None


def test_read_bom_designators_keeps_dni_numeric_and_annotated_rows(tmp_path):
    bom = write_bom(tmp_path / f"{JOB} AUDIT BOM.xlsx")
    names = read_bom_designators(bom, {})
    assert names.names["R5"] == "DNI"
    assert names.names["TP1"] == "5019"
    assert names.names["U1"] == "STM32F091RCT6"
    assert names.conflicts == ()


# --- section 4: generation end to end --------------------------------------------------

def test_prepare_builds_both_sides_from_program_origin_and_marks(tmp_path):
    plan = plan_for(build_job(tmp_path))
    top, bottom = plan.top, plan.bottom
    assert top.frame_status is FrameStatus.PROGRAM_ORIGIN and top.fiducial_status is FiducialStatus.PROGRAM_MARKS
    assert bottom.frame.mirrored_x and bottom.fiducial_status is FiducialStatus.PROGRAM_MARKS
    assert top.flags == () and bottom.flags == ()
    assert [(r.ref_des, r.part_name) for r in top.rows] == [
        ("TP1", "5019"), ("R5", "DNI"), ("C1", "GRM155R71C104KA88D"), ("C2", "GRM155R71C104KA88D"),
        ("Y1", "Q27-120-JXS11P4-12-"), ("R1", "RC0402FR-0710KL"), ("R2", "RC0402FR-0710KL"),
        ("U1", "STM32F091RCT6"), ("Q9", "TSC497CXRFG")]
    r1 = next(r for r in top.rows if r.ref_des == "R1")
    assert (r1.x_mil, r1.y_mil, r1.angle_deg) == (0.0, 2250.0, 0)
    assert {r.part_name for r in bottom.rows} == {"ERA-3AEB4532V"}
    assert plan.default_bottom_start() == 4
    assert plan.default_sides() == (BoardSide.TOP, BoardSide.BOTTOM)


def test_render_numbers_fiducials_across_sides(tmp_path):
    plan = plan_for(build_job(tmp_path))
    top_text = render_mirtec(plan.top, 1)
    bottom_text = render_mirtec(plan.bottom, plan.default_bottom_start())
    assert top_text.endswith("\r\n") and "\n" not in top_text.replace("\r\n", "")
    assert "TP1\t\t5019\t\t\t600.000000\t\t2850.000000\t\t0\r\n" in top_text
    assert top_text.splitlines()[-3:] == [
        "FID1\t\tFIDUCIAL\t\t-500.000000\t\t3000.000000\t\t0",
        "FID2\t\tFIDUCIAL\t\t900.000000\t\t3000.000000\t\t0",
        "FID3\t\tFIDUCIAL\t\t-500.000000\t\t-100.000000\t\t0"]
    assert bottom_text.splitlines()[-2:] == [
        "FID4\t\tFIDUCIAL\t\t-400.000000\t\t900.000000\t\t0",
        "FID5\t\tFIDUCIAL\t\t300.000000\t\t900.000000\t\t0"]


def test_without_a_program_root_coordinates_stay_in_cad_origin(tmp_path):
    plan = plan_for(build_job(tmp_path), program_root=None)
    assert plan.notices and "HLC program folder is not set" in plan.notices[0]
    assert plan.top.frame_status is FrameStatus.CAD_ORIGIN
    assert plan.top.fiducial_status is FiducialStatus.CAD_ROWS
    assert plan.top.fiducials == ((100.0, 100.0), (2900.0, 3100.0))
    r1 = next(r for r in plan.top.rows if r.ref_des == "R1")
    assert (r1.x_mil, r1.y_mil) == (1000.0, 2000.0)
    assert plan.default_sides() == (BoardSide.TOP, BoardSide.BOTTOM)


def test_a_side_without_a_program_is_left_out_by_default(tmp_path):
    plan = plan_for(build_job(tmp_path, programs="top"))
    assert plan.default_sides() == (BoardSide.TOP,)
    assert plan.bottom.frame_status is FrameStatus.CAD_ORIGIN
    assert plan.bottom.flags[0].kind is FlagKind.FRAME_UNAVAILABLE


def test_missing_program_folder_is_a_notice(tmp_path):
    plan = plan_for(build_job(tmp_path, programs="none"))
    assert any("No program folder" in n for n in plan.notices)
    assert plan.top.frame_status is FrameStatus.CAD_ORIGIN


def test_article_choice_round_trip(tmp_path):
    job = build_job(tmp_path, article_copy=True)
    pending = prepare_aoi_export(request_for(job))
    assert isinstance(pending, ProgramArticlePending)
    base = plan_for(job, article_chosen=True, article=None)
    assert base.top.frame.offset_x_mil == -1000.0
    second = plan_for(job, article_chosen=True, article="2ND")
    assert second.top.frame.offset_x_mil == -2000.0
    assert second.bottom.frame_status is FrameStatus.CAD_ORIGIN


def test_prepare_passes_xy_questions_back_to_the_operator(tmp_path):
    job = build_job(tmp_path)
    write_xyrs(job.job_dir / "second_XYRS.csv", TOP_ROWS)
    assert isinstance(prepare_aoi_export(request_for(job)), XyPending)
    plan = plan_for(job, xy_path=job.xy_path)
    assert plan.xy_path == job.xy_path


def test_fractional_angle_stops_the_build(tmp_path):
    job = build_job(tmp_path)
    write_xyrs(job.xy_path, [("R1", "RC0402FR-0710KL", 1, 2, 45.5, "T")])
    with pytest.raises(AoiBuildError) as caught:
        prepare_aoi_export(request_for(job))
    assert caught.value.problems[0][0] == "R1"


def test_designator_missing_from_bom_is_flagged(tmp_path):
    job = build_job(tmp_path)
    write_bom(job.stored_bom, [line for line in BOM_LINES if line[1] != "Q9"])
    plan = plan_for(job)
    flag = next(f for f in plan.top.flags if f.kind is FlagKind.MISSING_FROM_BOM)
    assert flag.designators == ("Q9",)
    assert next(r for r in plan.top.rows if r.ref_des == "Q9").part_name == "TSC497CXRFG"


def test_bom_conflict_is_flagged_on_its_side(tmp_path):
    job = build_job(tmp_path)
    write_bom(job.stored_bom, BOM_LINES + [("DNI", "R1")])
    plan = plan_for(job)
    assert next(r for r in plan.top.rows if r.ref_des == "R1").part_name == "DNI"
    assert [f.designators for f in plan.top.flags if f.kind is FlagKind.BOM_CONFLICT] == [("R1",)]
    assert len(plan.bom_conflicts) == 1


# --- section 6: two-phase write --------------------------------------------------------

def test_write_creates_both_files(tmp_path):
    plan = plan_for(build_job(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    report = write_aoi_files(plan, out)
    assert [p.name for p in report.written] == [f"T-{JOB}-XY.txt", f"B-{JOB}-XY.txt"]
    assert report.replaced == ()
    assert (out / f"T-{JOB}-XY.txt").read_bytes() == render_mirtec(plan.top, 1).encode("ascii")
    assert planned_outputs(plan, out) == list(report.written)


def test_write_refuses_to_overwrite_without_permission(tmp_path):
    plan = plan_for(build_job(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    write_aoi_files(plan, out)
    with pytest.raises(AoiDestinationExists) as caught:
        write_aoi_files(plan, out)
    assert len(caught.value.existing) == 2
    report = write_aoi_files(plan, out, overwrite=True)
    assert len(report.replaced) == 2


def test_write_honours_the_bottom_start_override_and_side_choice(tmp_path):
    plan = plan_for(build_job(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    report = write_aoi_files(plan, out, bottom_start=7, sides=(BoardSide.BOTTOM,))
    assert [p.name for p in report.written] == [f"B-{JOB}-XY.txt"]
    text = (out / f"B-{JOB}-XY.txt").read_text()
    assert "FID7\t\tFIDUCIAL" in text and "FID8\t\tFIDUCIAL" in text


def test_write_rejects_managed_storage_and_empty_selection(tmp_path):
    plan = plan_for(build_job(tmp_path))
    storage = tmp_path / "storage"
    with pytest.raises(AoiDestinationRejected):
        write_aoi_files(plan, storage, forbidden_roots=(storage,))
    with pytest.raises(AoiDestinationRejected):
        write_aoi_files(plan, tmp_path, sides=())
    with pytest.raises(AoiDestinationRejected):
        write_aoi_files(plan, tmp_path / "missing")


def test_rename_failure_removes_temp_files_and_names_what_was_written(tmp_path, monkeypatch):
    plan = plan_for(build_job(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    real_replace = export_module.os.replace
    calls = {"n": 0}

    def failing_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("locked by the AOI PC")
        return real_replace(src, dst)

    monkeypatch.setattr(export_module.os, "replace", failing_replace)
    with pytest.raises(AoiWriteError) as caught:
        write_aoi_files(plan, out)
    assert [p.name for p in caught.value.written] == [f"T-{JOB}-XY.txt"]
    assert list(out.glob(".*.tmp")) == []
    assert sorted(p.name for p in out.iterdir()) == [f"T-{JOB}-XY.txt"]


def test_failure_while_writing_temps_touches_no_final_file(tmp_path, monkeypatch):
    plan = plan_for(build_job(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    real_fsync = export_module.os.fsync
    calls = {"n": 0}

    def failing_fsync(fd):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_fsync(fd)

    monkeypatch.setattr(export_module.os, "fsync", failing_fsync)
    with pytest.raises(AoiWriteError) as caught:
        write_aoi_files(plan, out)
    assert caught.value.written == ()
    assert list(out.iterdir()) == []


# --- section 5: check mode ---------------------------------------------------------------

def test_compare_finds_no_difference_with_its_own_output(tmp_path):
    plan = plan_for(build_job(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    write_aoi_files(plan, out)
    assert compare_aoi(plan.top, 1, out / f"T-{JOB}-XY.txt").is_identical
    assert compare_aoi(plan.bottom, 4, out / f"B-{JOB}-XY.txt").is_identical


def test_compare_lists_each_kind_of_difference(tmp_path):
    plan = plan_for(build_job(tmp_path))
    lines = render_mirtec(plan.top, 1).splitlines()
    edited = []
    for line in lines:
        if line.startswith("R1\t"):
            line = line.replace("0.000000", "12.000000", 1)
        elif line.startswith("C1\t"):
            line = line.replace("GRM155R71C104KA88D", "GRM155R71C104KA99D")
        elif line.startswith("U1\t"):
            continue
        edited.append(line)
    edited.append(align_fields(["R99", "EXTRA", "1.000000", "1.000000", "0"]))
    existing = tmp_path / f"T-{JOB}-XY.txt"
    existing.write_text("\r\n".join(edited) + "\r\n")
    result = compare_aoi(plan.top, 1, existing)
    assert result.missing_in_existing == ("U1",)
    assert result.extra_in_existing == ("R99",)
    assert {(d.ref_des, d.kind) for d in result.differences} == {("R1", "position"), ("C1", "part name")}


def test_parse_mirtec_rejects_a_malformed_line(tmp_path):
    path = tmp_path / "T-x-XY.txt"
    path.write_text("R1\t\tPART\t\t1.0\r\n")
    with pytest.raises(AoiReadError):
        parse_mirtec(path)


# --- section 7: operator messages --------------------------------------------------------

def test_aoi_errors_render_with_reason_codes(tmp_path):
    payload = render(XyParseError(tmp_path / "x.csv", ((3, "R2: X/Y is not a number"),)))
    assert payload.reason_code == "AOI_XY_PARSE"
    assert payload.detail == [("line 3", "R2: X/Y is not a number")]
    write_error = AoiWriteError(tmp_path, PermissionError("locked"), (tmp_path / "T.txt",), ())
    assert ("written", str(tmp_path / "T.txt")) in render(write_error).detail
