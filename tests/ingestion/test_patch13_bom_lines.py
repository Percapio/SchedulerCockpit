"""Patch 13: BOM lines — continuation rows and the *DNI* suffix (Architecture/20261114-Patch13.md)."""

from pathlib import Path

import openpyxl
from openpyxl.styles import Font
import pytest

from cockpit.ingestion.parsers.audit_bom import (
    CLEANING_RULES_VERSION,
    CONSEQUENTIAL_ACTIONS,
    CleanedRow,
    ContinuationJoined,
    DesignatorRejected,
    DesignatorRepeatDropped,
    DesignatorsWithoutPartNumber,
    NoteExtracted,
    PartNumberSplit,
    UnknownMountTypeSkipped,
    find_number_key,
    is_dni_line_text,
    mount_gate_actions,
    parse,
    read_cleaned_sheet,
    read_raw_rows,
    split_part_number,
    summarise_cleaning,
)
from cockpit.services import requirements
from cockpit.services.requirements import (
    DEFAULT_VOCABULARY,
    EXTRACTOR_VERSION,
    ScanInputs,
    is_dni_line,
    read_bom_units,
    scan_fingerprint,
)
from cockpit.ui.widgets.cleaning_dialogs import describe_cleaning_action


HEADER = ["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT"]


def _bom(tmp_path: Path, rows: list[list[object]], struck_rows: tuple[int, ...] = ()) -> Path:
    path = tmp_path / "B123456 AUDIT BOM.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    for sheet_row in struck_rows:
        for cell in ws[sheet_row]:
            cell.font = Font(strike=True)
    wb.save(str(path))
    wb.close()
    return path


def _items(path: Path) -> dict[str, tuple[str, ...]]:
    return {item.component_mpn: item.ref_des_list for item in parse(path).items}


def _actions(path: Path) -> list:
    return [
        action
        for row in read_cleaned_sheet(path).rows
        if isinstance(row, CleanedRow)
        for action in row.actions
    ]


# ---------------------------------------------------------------------------
# §2 Continuation rows
# ---------------------------------------------------------------------------

def test_continuation_row_joins_its_line(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A", "R1, R2", "Res", "SMT"],
        ["", "", "R3, R4", "", ""],
        [2, "MPN-B", "C1", "Cap", "SMT"],
    ])

    result = parse(path)

    assert {i.component_mpn: i.ref_des_list for i in result.items} == {
        "MPN-A": ("R1", "R2", "R3", "R4"),
        "MPN-B": ("C1",),
    }
    assert result.raw_row_count == 3
    consequential = summarise_cleaning(result.cleaning).consequential
    assert ContinuationJoined(sheet_row=2, continuation_rows=[3], designators_joined=2) in consequential


def test_run_of_six_continuations_repeating_find_number_and_mount_type(tmp_path):
    path = _bom(tmp_path, [
        [7, "MPN-A", "R1", "Res", "SMT"],
        [7, "", "R2", "", "SMT"],
        ["7", "", "R3", "", ""],
        ["7.0", "", "R4", "", "smd"],
        ["", "", "R5", "", ""],
        ["", "", "R6", "", "S"],
        ["", "", "R7", "", ""],
    ])

    result = parse(path)

    assert [i.ref_des_list for i in result.items] == [("R1", "R2", "R3", "R4", "R5", "R6", "R7")]
    assert result.raw_row_count == 7
    joined = [a for a in _actions(path) if isinstance(a, ContinuationJoined)]
    assert joined == [ContinuationJoined(sheet_row=2, continuation_rows=[3, 4, 5, 6, 7, 8], designators_joined=6)]


def test_designator_already_on_the_line_is_dropped_once_across_the_run(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A", "R1, R2", "Res", "SMT"],
        ["", "", "R2, R3", "", ""],
        ["", "", "R3", "", ""],
    ])

    assert _items(path) == {"MPN-A": ("R1", "R2", "R3")}
    actions = _actions(path)
    assert DesignatorRepeatDropped(sheet_row=3, designator="R2") in actions
    assert DesignatorRepeatDropped(sheet_row=4, designator="R3") in actions
    assert ContinuationJoined(sheet_row=2, continuation_rows=[3, 4], designators_joined=1) in actions


def test_continuation_notes_join_the_line_and_its_x_ray_note_covers_the_whole_list(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A", "R1, R2 (hand solder)", "Res", "SMT"],
        ["", "", "R3, R4 (please x-ray)", "", ""],
    ])

    result = parse(path)
    assert [n.text for n in result.notes] == ["hand solder", "please x-ray"]

    units = read_bom_units(path).value
    xray = [u for u in units if "please x-ray" in u.text]
    assert len(xray) == 1
    assert set(xray[0].designators) == {"R1", "R2", "R3", "R4"}


@pytest.mark.parametrize(
    "rows, struck, reported_row",
    [
        pytest.param(
            [[1, "MPN-A", "R1", "Res", "SMT"], [2, "", "R2", "", ""]],
            (), 3, id="another Find#",
        ),
        pytest.param(
            [[1, "MPN-A", "R1", "Res", "SMT"], ["", "", "R2", "", "THT"]],
            (), 3, id="another mount type",
        ),
        pytest.param(
            [[1, "MPN-A", "R1", "Res", "SMT"], [2, "MPN-B", "R9", "Res", "SMT"], ["", "", "R2", "", ""]],
            (3,), 4, id="after a removed line",
        ),
        pytest.param(
            [["", "", "R2", "", ""], [1, "MPN-A", "R1", "Res", "SMT"]],
            (), 2, id="first after the header",
        ),
        pytest.param(
            [[1, "MPN-A", "R1", "Res", "SMT"], ["", "", "", "heading", ""], ["", "", "R2", "", ""]],
            (), 4, id="after a row with no part number and no designators",
        ),
        pytest.param(
            [[1, "MPN-A", "R1", "Res", "SMT"], [2, "", "R3", "", ""], ["", "", "R2", "", ""]],
            (), 4, id="after a row that continues nothing",
        ),
    ],
)
def test_rows_that_continue_nothing_are_reported_and_left_out(tmp_path, rows, struck, reported_row):
    path = _bom(tmp_path, rows, struck_rows=struck)

    assert _items(path)["MPN-A"] == ("R1",)
    reported = [a for a in _actions(path) if isinstance(a, DesignatorsWithoutPartNumber)]
    assert DesignatorsWithoutPartNumber(sheet_row=reported_row, designator_count=1) in reported
    assert not [a for a in _actions(path) if isinstance(a, ContinuationJoined)]


@pytest.mark.parametrize("line_mount", ["", "DNI"])
def test_unrecognised_mount_type_continues_no_line_whose_mount_type_reads_as_none(tmp_path, line_mount):
    path = _bom(tmp_path, [
        [1, "MPN-A", "R1", "Res", line_mount],
        ["", "", "R2", "", "HAND PLACE"],
    ])

    actions = _actions(path)
    assert DesignatorsWithoutPartNumber(sheet_row=3, designator_count=1) in actions
    assert not [a for a in actions if isinstance(a, ContinuationJoined)]


def test_row_after_a_rejected_designator_row_continues_nothing(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A", "D6, D 19", "Diode", "SMT"],
        ["", "", "R2", "", ""],
    ])

    rows = read_cleaned_sheet(path).rows
    assert isinstance(rows[0], DesignatorRejected)
    assert DesignatorsWithoutPartNumber(sheet_row=3, designator_count=1) in rows[1].actions


def test_row_with_no_part_number_and_no_designators_is_left_as_it_is(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A", "R1", "Res", "SMT"],
        ["", "", "", "just a heading", ""],
    ])

    rows = read_cleaned_sheet(path).rows
    assert len(rows) == 2
    assert not [a for a in rows[1].actions if isinstance(a, (ContinuationJoined, DesignatorsWithoutPartNumber))]


def test_continuation_takes_its_lines_dni_and_pcb_status(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A*DNI*", "R1", "Res", "SMT"],
        ["", "", "R2, R3", "", ""],
        [2, "PCB", "PCB1", "Board", "THT"],
        ["", "", "PCB2", "", ""],
        [3, "MPN-B", "C1", "Cap", "SMT"],
    ])

    result = parse(path)

    assert {i.component_mpn for i in result.items} == {"MPN-B"}
    assert result.dni_designators == frozenset({"R1", "R2", "R3"})
    assert result.excluded_dni_count == 1
    assert result.excluded_pcb_count == 1


def test_dni_mount_type_continuation_joins_a_dni_mount_type_line(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A", "R1", "Res", "DNI"],
        ["", "", "R2", "", "dni"],
        [2, "MPN-B", "C1", "Cap", "SMT"],
    ])

    result = parse(path)

    assert {i.component_mpn for i in result.items} == {"MPN-B"}
    assert result.dni_designators == frozenset({"R1", "R2"})
    assert ContinuationJoined(sheet_row=2, continuation_rows=[3], designators_joined=1) in _actions(path)


def test_second_ops_rows_keep_continuations_as_rows_of_their_own(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A", "R1, R2", "Res", "SMT"],
        ["", "", "R3, R4", "", ""],
        [2, "MPN-B", "C1", "Cap", "SMT"],
    ])

    raw = read_raw_rows(path)

    assert [r.part_number for r in raw] == ["MPN-A", "", "MPN-B"]
    assert raw[1].designators == ["R3", "R4"]


@pytest.mark.parametrize(
    "text, key",
    [("", ""), ("   ", ""), ("12", "12"), ("12.0", "12"), (" 7.00 ", "7"), ("12a", "12A")],
)
def test_find_number_key(text, key):
    assert find_number_key(text) == key


# ---------------------------------------------------------------------------
# §3 The DNI predicate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "part_number, mount_type",
    [
        ("DNI", ""), ("*DNI*", ""), ("*DNI", ""), ("DNI*", ""), ("X*DNI*", ""),
        (" x *dni* ", ""), ("MPN-1", "dni"), ("MPN-1", " DNI "), ("", "DNI"),
    ],
)
def test_dni_lines(part_number, mount_type):
    assert is_dni_line_text(part_number, mount_type)


@pytest.mark.parametrize(
    "part_number, mount_type",
    [
        ("NA", ""), ("NONE", ""), ("DNIX", ""), ("X*DNI", ""), ("X *DNI* REV", ""),
        ("MPN-1", "SMT"), ("", ""), ("DO NOT LOAD", ""),
    ],
)
def test_live_lines(part_number, mount_type):
    assert not is_dni_line_text(part_number, mount_type)


def test_split_part_number_keeps_a_trailing_dni_marker():
    assert split_part_number("ABC123 *DNI*") == PartNumberSplit(part_number="ABC123 *DNI*", note=None)
    assert split_part_number(" ABC123   *dni*  ") == PartNumberSplit(part_number="ABC123   *dni*", note=None)
    assert split_part_number("ABC123*DNI*") == PartNumberSplit(part_number="ABC123*DNI*", note=None)
    assert split_part_number("ABC123 *SEE NOTE*") == PartNumberSplit(part_number="ABC123", note="SEE NOTE*")


def test_spelled_dni_suffixes_are_dni_lines_end_to_end(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A *DNI*", "R1", "Res", "SMT"],
        [2, "MPN-B   *dni*", "R2", "Res", "SMT"],
        [3, "*DNI", "J1, J2", "Header", "THT"],
        [4, "DNI", "R5", "Res", ""],
        [5, "MPN-C", "C1", "Cap", "SMT"],
        [6, "NA", "R6", "Res", "SMT"],
    ])

    result = parse(path)

    assert {i.component_mpn for i in result.items} == {"MPN-C", "NA"}
    assert result.excluded_dni_count == 4
    assert result.dni_designators == frozenset({"R1", "R2", "J1", "J2", "R5"})
    assert not [a for a in result.cleaning.actions if isinstance(a, NoteExtracted)]

    for row in read_cleaned_sheet(path).rows:
        assert is_dni_line(row) == set(row.designators).issubset(result.dni_designators)


def test_mount_gate_says_nothing_about_a_dni_suffixed_line(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A*DNI*", "R1", "Res", "HAND PLACE"],
        [2, "MPN-B", "R2", "Res", "HAND PLACE"],
    ])

    assert mount_gate_actions(read_cleaned_sheet(path)) == [
        UnknownMountTypeSkipped(sheet_row=3, part_number="MPN-B", mount_text="HAND PLACE")
    ]


def test_a_dni_suffixed_lines_note_yields_no_unit(tmp_path):
    path = _bom(tmp_path, [
        [1, "MPN-A *DNI*", "R1 (please x-ray)", "Res", "SMT"],
        [2, "MPN-B", "R2 (hand solder)", "Res", "SMT"],
    ])

    texts = [u.text for u in read_bom_units(path).value]
    assert not any("please x-ray" in t for t in texts)
    assert any("hand solder" in t for t in texts)


# ---------------------------------------------------------------------------
# §2 Reporting, §4 Versions
# ---------------------------------------------------------------------------

def test_new_actions_are_consequential():
    assert ContinuationJoined in CONSEQUENTIAL_ACTIONS
    assert DesignatorsWithoutPartNumber in CONSEQUENTIAL_ACTIONS


@pytest.mark.parametrize(
    "action, text",
    [
        (ContinuationJoined(8, [9], 188), "Row 9 continues this line; its 188 designators were read with it."),
        (
            ContinuationJoined(8, [9, 10, 11, 12, 13, 14], 1206),
            "Rows 9–14 continue this line; their 1,206 designators were read with it.",
        ),
        (ContinuationJoined(8, [9, 11], 1), "Rows 9 and 11 continue this line; their 1 designator was read with it."),
        (ContinuationJoined(8, [9, 11, 13], 3), "Rows 9, 11 and 13 continue this line; their 3 designators were read with it."),
        (ContinuationJoined(8, [9], 0), "Row 9 continues this line; every designator on it was already on the line."),
        (
            DesignatorsWithoutPartNumber(5, 188),
            "188 designators with no part number, not continuing the line above; the parts were left out.",
        ),
        (
            DesignatorsWithoutPartNumber(5, 1),
            "1 designator with no part number, not continuing the line above; the part was left out.",
        ),
    ],
)
def test_describe_new_actions(action, text):
    assert describe_cleaning_action(action) == (str(action.sheet_row), text)


def test_versions_change_the_scan_fingerprint(monkeypatch):
    assert CLEANING_RULES_VERSION == 2
    assert EXTRACTOR_VERSION == 2
    inputs = ScanInputs(Path("notes.docx"), "n1", Path("bom.xlsx"), "b1", None, DEFAULT_VOCABULARY)
    current = scan_fingerprint(inputs)

    monkeypatch.setattr(requirements, "EXTRACTOR_VERSION", 1)
    assert scan_fingerprint(inputs) != current
    monkeypatch.setattr(requirements, "EXTRACTOR_VERSION", 2)
    monkeypatch.setattr(requirements, "CLEANING_RULES_VERSION", 1)
    assert scan_fingerprint(inputs) != current
