import importlib
import openpyxl
from openpyxl.styles import Font
import pytest

import cockpit.ingestion.parsers.audit_bom as audit_bom_mod
from cockpit.ingestion.errors import GatekeeperViolation, MalformedBomError
from cockpit.ingestion.gatekeeper import validate as gatekeeper_validate
from cockpit.ingestion.parsers.audit_bom import (
    BomColumnMap,
    BomNote,
    CLEANING_RULES_VERSION,
    CANONICAL_COLUMNS,
    ChosenSheet,
    CleanedRow,
    CleaningReport,
    ColumnName,
    ConsumedColumnDuplicated,
    ConsumedColumnMissing,
    DesignatorPlaceholder,
    DesignatorRepeatDropped,
    DesignatorsStruck,
    EnclosingQuotesRemoved,
    FreeTextInRefDes,
    HiddenSheetRead,
    LineRemovedByStrike,
    MountType,
    MountTypeReplacedByStrike,
    NoSheetHasHeader,
    NoteExtracted,
    PartNumberReplacedByStrike,
    RefDesKind,
    RefDesSplit,
    SheetCell,
    SheetsAmbiguous,
    StruckTextRemoved,
    TextRun,
    TokenRejected,
    UnknownMountTypeSkipped,
    build_cleaning_report,
    choose_bom_sheet,
    clean_row,
    describe_routine_cleaning,
    mount_gate_actions,
    mount_type_of,
    parse,
    probe_sheets,
    read_cell,
    read_cleaned_sheet,
    read_raw_rows,
    resolve_bom_header,
    split_ref_des,
    summarise_cleaning,
    unwrap_enclosing_quotes,
)
from cockpit.ingestion.roles import SourceRole, role_of
from cockpit.ui.error_messages import render as render_error


CANONICAL_HEADER = [
    "Find#", "PartNum", "Count", "MSL level", "Date code", "Baked date",
    "Ref_Des", "Package", "Description", "SMT/THT", "Qty Need", "Qty On hand",
    "Qty short", "comment",
]

FIVE_CONSUMED_HEADER = [
    "Find#", "PartNum", "Ref_Des", "Description", "SMT/THT",
]


def _make_workbook(sheets_spec: list[tuple[str, bool, list[list]]]) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    first = True
    for name, visible, rows in sheets_spec:
        if first:
            ws = wb.active
            ws.title = name
            first = False
        else:
            ws = wb.create_sheet(title=name)
        ws.sheet_state = "visible" if visible else "hidden"
        for r in rows:
            ws.append(r)
    return wb


# ---- 1. probe_sheets and choose_bom_sheet ----------------------------------

def test_probe_and_choose_single_sheet_any_name():
    wb = _make_workbook([
        ("B123456 AUDIT", True, [CANONICAL_HEADER]),
    ])
    probes = probe_sheets(wb)
    assert len(probes) == 1
    assert probes[0].sheet_name == "B123456 AUDIT"
    assert probes[0].is_complete is True
    assert probes[0].header_row == 1
    chosen = choose_bom_sheet(probes, "B123456")
    assert isinstance(chosen, ChosenSheet)
    assert chosen.sheet_name == "B123456 AUDIT"
    assert chosen.header_row == 1


def test_probe_and_choose_header_under_banner_row():
    wb = _make_workbook([
        ("Sheet1", True, [
            ["Company Banner", "", ""],
            ["", "", ""],
            CANONICAL_HEADER,
        ]),
    ])
    probes = probe_sheets(wb)
    chosen = choose_bom_sheet(probes, "B123456")
    assert isinstance(chosen, ChosenSheet)
    assert chosen.header_row == 3


def test_choose_complete_preferred_over_incomplete():
    wb = _make_workbook([
        ("Summary", True, [["Find#", "PartNum", "Count"]]),
        ("FullBOM", True, [CANONICAL_HEADER]),
    ])
    probes = probe_sheets(wb)
    assert probes[0].is_complete is False
    assert probes[1].is_complete is True
    chosen = choose_bom_sheet(probes, "B123456")
    assert isinstance(chosen, ChosenSheet)
    assert chosen.sheet_name == "FullBOM"


def test_choose_two_complete_resolved_by_audit_bom_and_job_number_and_ambiguous():
    # Resolved by AUDIT BOM
    wb1 = _make_workbook([
        ("Other Sheet", True, [CANONICAL_HEADER]),
        ("  audit bom ", True, [CANONICAL_HEADER]),
    ])
    chosen1 = choose_bom_sheet(probe_sheets(wb1), "B123456")
    assert isinstance(chosen1, ChosenSheet)
    assert chosen1.sheet_name == "  audit bom "

    # Resolved by exact job number
    wb2 = _make_workbook([
        ("Other Sheet", True, [CANONICAL_HEADER]),
        ("B123456", True, [CANONICAL_HEADER]),
    ])
    chosen2 = choose_bom_sheet(probe_sheets(wb2), "B123456")
    assert isinstance(chosen2, ChosenSheet)
    assert chosen2.sheet_name == "B123456"

    # Resolved by job number substring
    wb3 = _make_workbook([
        ("Other Sheet", True, [CANONICAL_HEADER]),
        ("Job B123456 Rev A", True, [CANONICAL_HEADER]),
    ])
    chosen3 = choose_bom_sheet(probe_sheets(wb3), "B123456")
    assert isinstance(chosen3, ChosenSheet)
    assert chosen3.sheet_name == "Job B123456 Rev A"

    # Left ambiguous when neither matches
    wb4 = _make_workbook([
        ("Sheet A", True, [CANONICAL_HEADER]),
        ("Sheet B", True, [CANONICAL_HEADER]),
    ])
    res4 = choose_bom_sheet(probe_sheets(wb4), "B123456")
    assert isinstance(res4, SheetsAmbiguous)
    assert res4.candidates == ["Sheet A", "Sheet B"]


def test_choose_non_job_declared_token_uses_only_first_rule():
    # Declared token "AUDIT" does not match JOB_NUMBER_GRAMMAR, so substring/exact job rules do not fire
    wb = _make_workbook([
        ("AUDIT", True, [CANONICAL_HEADER]),
        ("Other", True, [CANONICAL_HEADER]),
    ])
    res = choose_bom_sheet(probe_sheets(wb), "AUDIT")
    assert isinstance(res, SheetsAmbiguous)
    assert res.candidates == ["AUDIT", "Other"]

    # First rule ("AUDIT BOM") still fires even with non-job declared token
    wb_first = _make_workbook([
        ("AUDIT BOM", True, [CANONICAL_HEADER]),
        ("AUDIT", True, [CANONICAL_HEADER]),
    ])
    chosen = choose_bom_sheet(probe_sheets(wb_first), "AUDIT")
    assert isinstance(chosen, ChosenSheet)
    assert chosen.sheet_name == "AUDIT BOM"


def test_choose_hidden_sheet_passed_over_when_visible_same_completeness():
    wb = _make_workbook([
        ("AUDIT BOM", False, [CANONICAL_HEADER]),
        ("Visible Sheet", True, [CANONICAL_HEADER]),
    ])
    chosen = choose_bom_sheet(probe_sheets(wb), "B123456")
    assert isinstance(chosen, ChosenSheet)
    assert chosen.sheet_name == "Visible Sheet"
    assert chosen.is_visible is True


def test_choose_hidden_sheet_read_and_reported_when_only_one(tmp_path):
    path = tmp_path / "B123456 AUDIT BOM.xlsx"
    wb = _make_workbook([
        ("Notes", True, [["Just some notes"]]),
        ("Hidden BOM", False, [
            CANONICAL_HEADER,
            [1, "MPN-1", 1, "", "", "", "R1", "0402", "Res", "SMT", 1, 1, 0, ""],
        ]),
    ])
    wb.save(str(path))
    wb.close()

    res = parse(path)
    assert res.sheet_name_used == "Hidden BOM"
    summary = summarise_cleaning(res.cleaning)
    assert HiddenSheetRead(sheet_name="Hidden BOM") in summary.consequential


def test_choose_hidden_complete_preferred_over_visible_incomplete():
    wb = _make_workbook([
        ("Visible Incomplete", True, [["Find#", "PartNum"]]),
        ("Hidden Complete", False, [CANONICAL_HEADER]),
    ])
    chosen = choose_bom_sheet(probe_sheets(wb), "B123456")
    assert isinstance(chosen, ChosenSheet)
    assert chosen.sheet_name == "Hidden Complete"
    assert chosen.is_visible is False


def test_no_sheet_has_header_lists_every_sheet_visibility():
    wb = _make_workbook([
        ("Vis1", True, [["A", "B"]]),
        ("Hid2", False, [["C", "D"]]),
    ])
    res = choose_bom_sheet(probe_sheets(wb), "B123456")
    assert isinstance(res, NoSheetHasHeader)
    assert [(p.sheet_name, p.is_visible) for p in res.probes] == [
        ("Vis1", True),
        ("Hid2", False),
    ]


# ---- 2. resolve_bom_header -------------------------------------------------

def test_resolve_bom_header_variants_blanks_reordered_missing_duplicated():
    # Spelling and case variants (Qty Short, COMMENT, QOH), blank labels, reordered columns
    labels = [
        "", "PARTNUM", "Find#", "Ref_Des", "  ", "Description", "SMT/THT",
        "Qty Short", "COMMENT", "QOH", "ExtraCol",
    ]
    res = resolve_bom_header(labels)
    assert isinstance(res, BomColumnMap)
    assert res.consumed[ColumnName.PART_NUMBER] == 1
    assert res.consumed[ColumnName.FIND_NUMBER] == 2
    assert res.consumed[ColumnName.REF_DES] == 3
    assert res.consumed[ColumnName.DESCRIPTION] == 5
    assert res.consumed[ColumnName.MOUNT_TYPE] == 6
    assert res.display[ColumnName.QTY_SHORT] == 7
    assert res.display[ColumnName.COMMENT] == 8
    assert res.ignored_labels == ["QOH", "ExtraCol"]

    # Missing consumed column named
    res_missing = resolve_bom_header(["Find#", "PartNum", "Ref_Des", "SMT/THT"])
    assert isinstance(res_missing, ConsumedColumnMissing)
    assert res_missing.missing == [ColumnName.DESCRIPTION]

    # Repeated consumed column
    res_dup = resolve_bom_header(["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT", "ref_des"])
    assert isinstance(res_dup, ConsumedColumnDuplicated)
    assert res_dup.duplicated == [ColumnName.REF_DES]

    # Repeated display column uses first position
    res_disp_dup = resolve_bom_header([
        "Find#", "PartNum", "Ref_Des", "Description", "SMT/THT",
        "Comment", "Qty Need", "COMMENT",
    ])
    assert isinstance(res_disp_dup, BomColumnMap)
    assert res_disp_dup.display[ColumnName.COMMENT] == 5
    assert "COMMENT" in res_disp_dup.ignored_labels


# ---- 3. read_cell ----------------------------------------------------------

def test_read_cell_strikes_and_numbers():
    # Whole-cell strike
    c_whole = SheetCell(value="ABC", runs=[], is_cell_struck=True)
    assert read_cell(c_whole) == audit_bom_mod.CellReading(
        raw_text="ABC", visible_text="", struck_text="ABC"
    )

    # Run strike
    c_runs = SheetCell(
        value="OLD NEW",
        runs=[
            TextRun(text="OLD ", is_struck=True, has_own_font=True),
            TextRun(text="NEW", is_struck=False, has_own_font=True),
        ],
        is_cell_struck=False,
    )
    assert read_cell(c_runs) == audit_bom_mod.CellReading(
        raw_text="OLD NEW", visible_text="NEW", struck_text="OLD "
    )

    # Unformatted run inheriting cell's strike
    c_inherit = SheetCell(
        value="STRUCK KEPT",
        runs=[
            TextRun(text="STRUCK ", is_struck=False, has_own_font=False),
            TextRun(text="KEPT", is_struck=False, has_own_font=True),
        ],
        is_cell_struck=True,
    )
    assert read_cell(c_inherit) == audit_bom_mod.CellReading(
        raw_text="STRUCK KEPT", visible_text="KEPT", struck_text="STRUCK "
    )

    # Struck number
    c_num = SheetCell(value=42, runs=[], is_cell_struck=True)
    assert read_cell(c_num) == audit_bom_mod.CellReading(
        raw_text="42", visible_text="", struck_text="42"
    )


# ---- 4. unwrap_enclosing_quotes --------------------------------------------

def test_unwrap_enclosing_quotes_cases():
    # Straight and curly pairs
    assert unwrap_enclosing_quotes('"R1, R2"') == audit_bom_mod.QuoteUnwrap("R1, R2", True)
    assert unwrap_enclosing_quotes("'MPN-1'") == audit_bom_mod.QuoteUnwrap("MPN-1", True)
    assert unwrap_enclosing_quotes("\u201cCurly Double\u201d") == audit_bom_mod.QuoteUnwrap("Curly Double", True)
    assert unwrap_enclosing_quotes("\u2018Curly Single\u2019") == audit_bom_mod.QuoteUnwrap("Curly Single", True)

    # Doubled "" escapes inside straight quotes
    assert unwrap_enclosing_quotes('"0.1"" HEADER"') == audit_bom_mod.QuoteUnwrap('0.1" HEADER', True)

    # Inch mark untouched
    assert unwrap_enclosing_quotes('0.1"') == audit_bom_mod.QuoteUnwrap('0.1"', False)

    # Inner unescaped quote untouched
    assert unwrap_enclosing_quotes('"0.1" HEADER"') == audit_bom_mod.QuoteUnwrap('"0.1" HEADER"', False)
    assert unwrap_enclosing_quotes('"ON" side up, "OFF"') == audit_bom_mod.QuoteUnwrap('"ON" side up, "OFF"', False)

    # Single character
    assert unwrap_enclosing_quotes('"') == audit_bom_mod.QuoteUnwrap('"', False)
    assert unwrap_enclosing_quotes("S") == audit_bom_mod.QuoteUnwrap("S", False)


# ---- 5. split_ref_des (§4.4 table & case-sensitive repeats) ----------------

def test_split_ref_des_section_4_4_table_and_repeats():
    # Row 1: U1, ~~U2,~~ U3 -> after strike removal leaves "U1,  U3"
    r1 = split_ref_des("U1,  U3")
    assert isinstance(r1, RefDesSplit)
    assert r1.designators == ["U1", "U3"]
    assert r1.notes == []

    # Row 2: C1, C2,\nC3
    r2 = split_ref_des("C1, C2,\nC3")
    assert isinstance(r2, RefDesSplit)
    assert r2.designators == ["C1", "C2", "C3"]
    assert r2.notes == []

    # Row 3: \nQ1, Q2, Q3, Q4, Q5, Q6
    r3 = split_ref_des("\nQ1, Q2, Q3, Q4, Q5, Q6")
    assert isinstance(r3, RefDesSplit)
    assert r3.designators == ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"]
    assert r3.notes == []

    # Row 4: C1,  C2  ECO NOTE
    r4 = split_ref_des("C1,  C2  ECO NOTE")
    assert isinstance(r4, RefDesSplit)
    assert r4.designators == ["C1", "C2"]
    assert r4.notes == ["ECO NOTE"]

    # Row 5: J17, J18  - DNI
    r5 = split_ref_des("J17, J18  - DNI")
    assert isinstance(r5, RefDesSplit)
    assert r5.designators == ["J17", "J18"]
    assert r5.notes == ["- DNI"]

    # Row 6: SW1          after wash*
    r6 = split_ref_des("SW1          after wash*")
    assert isinstance(r6, RefDesSplit)
    assert r6.designators == ["SW1"]
    assert r6.notes == ["after wash"]

    # Row 7: THERMAL COMPOUND          *see ECO note #6*
    r7 = split_ref_des("THERMAL COMPOUND          *see ECO note #6*")
    assert isinstance(r7, RefDesSplit)
    assert r7.designators == []
    assert r7.kind == RefDesKind.FREE_TEXT
    assert r7.residual == "THERMAL COMPOUND"
    assert r7.notes == ["see ECO note #6"]

    # Row 8: N/A
    r8 = split_ref_des("N/A")
    assert isinstance(r8, RefDesSplit)
    assert r8.designators == []
    assert r8.kind == RefDesKind.PLACEHOLDER
    assert r8.residual == "N/A"

    # Row 9: C1, C2, C1
    r9 = split_ref_des("C1, C2, C1")
    assert isinstance(r9, RefDesSplit)
    assert r9.designators == ["C1", "C2"]
    assert r9.repeats == ["C1"]

    # Row 10: R1(no-close
    r10 = split_ref_des("R1(no-close")
    assert isinstance(r10, TokenRejected)

    # C1 and c1 are both kept (case-sensitive repeat check)
    r_case = split_ref_des("C1, c1, C1")
    assert isinstance(r_case, RefDesSplit)
    assert r_case.designators == ["C1", "c1"]
    assert r_case.repeats == ["C1"]


# ---- 6. mount_type_of ------------------------------------------------------

def test_mount_type_of_variants():
    assert mount_type_of("SMT") == MountType.SMT
    assert mount_type_of("THT") == MountType.THT
    assert mount_type_of("S") == MountType.SMT
    assert mount_type_of("T") == MountType.THT
    assert mount_type_of("SURFACE MOUNT") == MountType.SMT
    assert mount_type_of("  smt  ") == MountType.SMT
    assert mount_type_of("PCB") is None
    assert mount_type_of("DNI") is None
    assert mount_type_of("HAND PLACE") is None
    assert mount_type_of("") is None


# ---- 7. clean_row ----------------------------------------------------------

def _make_column_map(cols: list[ColumnName]) -> BomColumnMap:
    consumed = {c: idx for idx, c in enumerate(cols) if c in audit_bom_mod.CONSUMED_COLUMNS}
    display = {c: idx for idx, c in enumerate(cols) if c in audit_bom_mod.DISPLAY_COLUMNS}
    return BomColumnMap(consumed=consumed, display=display, ignored_labels=[])


def test_clean_row_removed_line():
    cmap = _make_column_map([
        ColumnName.FIND_NUMBER,
        ColumnName.PART_NUMBER,
        ColumnName.REF_DES,
        ColumnName.DESCRIPTION,
        ColumnName.MOUNT_TYPE,
    ])
    cells = [
        SheetCell("1"),
        SheetCell("MPN-OLD", is_cell_struck=True),
        SheetCell("R1"),
        SheetCell("Desc"),
        SheetCell("SMT"),
    ]
    res = clean_row(cells, 5, cmap)
    assert isinstance(res, CleanedRow)
    assert res.is_removed_line is True
    assert res.actions == [LineRemovedByStrike(sheet_row=5, struck_part_number="MPN-OLD")]


def test_clean_row_struck_mount_type_beside_wrapped_tht():
    cmap = _make_column_map([
        ColumnName.FIND_NUMBER,
        ColumnName.PART_NUMBER,
        ColumnName.REF_DES,
        ColumnName.DESCRIPTION,
        ColumnName.MOUNT_TYPE,
    ])
    cells = [
        SheetCell("1"),
        SheetCell("MPN-1"),
        SheetCell("J1"),
        SheetCell("Connector"),
        SheetCell(
            value='SMT "THT"',
            runs=[
                TextRun("SMT ", is_struck=True, has_own_font=True),
                TextRun('"THT"', is_struck=False, has_own_font=True),
            ],
        ),
    ]
    res = clean_row(cells, 3, cmap)
    assert isinstance(res, CleanedRow)
    assert res.cells[ColumnName.MOUNT_TYPE] == "THT"
    assert MountTypeReplacedByStrike(sheet_row=3, struck="SMT", kept=MountType.THT) in res.actions
    assert EnclosingQuotesRemoved(sheet_row=3, column=ColumnName.MOUNT_TYPE) in res.actions


def test_clean_row_struck_part_number_replacement_with_quotes_and_note():
    cmap = _make_column_map([
        ColumnName.FIND_NUMBER,
        ColumnName.PART_NUMBER,
        ColumnName.REF_DES,
        ColumnName.DESCRIPTION,
        ColumnName.MOUNT_TYPE,
    ])
    cells = [
        SheetCell('"10"'),
        SheetCell(
            value='OLD-MPN "NEW-MPN *alt approved"',
            runs=[
                TextRun("OLD-MPN ", is_struck=True, has_own_font=True),
                TextRun('"NEW-MPN *alt approved"', is_struck=False, has_own_font=True),
            ],
        ),
        SheetCell("R1 (top side)"),
        SheetCell("Resistor"),
        SheetCell("SMT"),
    ]
    res = clean_row(cells, 7, cmap)
    assert isinstance(res, CleanedRow)
    assert res.cells[ColumnName.FIND_NUMBER] == "10"
    assert res.cells[ColumnName.PART_NUMBER] == "NEW-MPN"
    assert PartNumberReplacedByStrike(sheet_row=7, struck="OLD-MPN", kept="NEW-MPN") in res.actions
    assert EnclosingQuotesRemoved(sheet_row=7, column=ColumnName.FIND_NUMBER) in res.actions
    assert EnclosingQuotesRemoved(sheet_row=7, column=ColumnName.PART_NUMBER) in res.actions
    assert [n.text for n in res.notes] == ["alt approved", "top side"]


def test_clean_row_struck_designators_mixed_with_struck_note_text():
    cmap = _make_column_map([
        ColumnName.FIND_NUMBER,
        ColumnName.PART_NUMBER,
        ColumnName.REF_DES,
        ColumnName.DESCRIPTION,
        ColumnName.MOUNT_TYPE,
    ])
    cells = [
        SheetCell("1"),
        SheetCell("MPN-1"),
        SheetCell(
            value="R1, R2, R3, (see note), R4",
            runs=[
                TextRun("R1, ", is_struck=False, has_own_font=True),
                TextRun("R2, R3, (see note), ", is_struck=True, has_own_font=True),
                TextRun("R4", is_struck=False, has_own_font=True),
            ],
        ),
        SheetCell("Resistor"),
        SheetCell("SMT"),
    ]
    res = clean_row(cells, 4, cmap)
    assert isinstance(res, CleanedRow)
    assert res.designators == ["R1", "R4"]
    struck_actions = [a for a in res.actions if isinstance(a, DesignatorsStruck)]
    assert struck_actions == [DesignatorsStruck(sheet_row=4, struck=["R2", "R3"])]


def test_clean_row_placeholder_and_free_text_reported_once():
    cmap = _make_column_map([
        ColumnName.FIND_NUMBER,
        ColumnName.PART_NUMBER,
        ColumnName.REF_DES,
        ColumnName.DESCRIPTION,
        ColumnName.MOUNT_TYPE,
    ])
    cells_ph = [
        SheetCell("1"),
        SheetCell("MPN-1"),
        SheetCell("TBD"),
        SheetCell("Desc"),
        SheetCell("SMT"),
    ]
    res_ph = clean_row(cells_ph, 2, cmap)
    assert isinstance(res_ph, CleanedRow)
    assert [a for a in res_ph.actions if isinstance(a, DesignatorPlaceholder)] == [
        DesignatorPlaceholder(sheet_row=2, text="TBD")
    ]

    cells_ft = [
        SheetCell("2"),
        SheetCell("MPN-2"),
        SheetCell("SEE DRAWING FOR LOCATION"),
        SheetCell("Desc"),
        SheetCell("SMT"),
    ]
    res_ft = clean_row(cells_ft, 3, cmap)
    assert isinstance(res_ft, CleanedRow)
    assert [a for a in res_ft.actions if isinstance(a, FreeTextInRefDes)] == [
        FreeTextInRefDes(sheet_row=3, text="SEE DRAWING FOR LOCATION")
    ]


# ---- 8. parse (§5.1 skips, §9 errors, duplicates, notes) -------------------

def test_parse_skips_and_notes_retention(tmp_path):
    path = tmp_path / "B123456 AUDIT BOM.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(CANONICAL_HEADER)
    # Row 2: Live SMT row with note
    ws.append([1, "MPN-SMT", 1, "", "", "", "R1 (note live)", "0402", "Res", "SMT", 1, 1, 0, ""])
    # Row 3: PCB row (silent skip, note kept)
    ws.append([2, "PCB", 1, "", "", "", "PCB1 (note pcb)", "", "Bare board", "", 1, 1, 0, ""])
    # Row 4: DNI row (silent skip, note NOT kept)
    ws.append([3, "DNI", 1, "", "", "", "R99 (note dni)", "0402", "Res", "SMT", 1, 1, 0, ""])
    # Row 5: Section heading (empty Find# and empty SMT/THT -> silent skip, note kept if any)
    ws.append(["", "DO NOT LOAD", "", "", "", "", "(heading note)", "", "", "", "", "", "", ""])
    # Row 6: Unknown mount type "HAND PLACE" (reported skip, note kept)
    ws.append([4, "MPN-HP", 1, "", "", "", "U1 (note hp)", "SOIC", "IC", "HAND PLACE", 1, 1, 0, ""])
    # Row 7: Removed line by strike sharing MPN-SMT with Row 2 -> does not fail DUPLICATE_MPN, note NOT kept
    ws.append([5, "MPN-SMT", 1, "", "", "", "R2 (note struck)", "0402", "Res", "SMT", 1, 1, 0, ""])
    ws.cell(row=7, column=2).font = Font(strike=True)

    wb.save(str(path))
    wb.close()

    res = parse(path)
    assert len(res.items) == 1
    assert res.items[0].component_mpn == "MPN-SMT"
    assert res.excluded_dni_count == 1
    summary = summarise_cleaning(res.cleaning)
    assert [a for a in summary.consequential if isinstance(a, UnknownMountTypeSkipped)] == [
        UnknownMountTypeSkipped(sheet_row=6, part_number="MPN-HP", mount_text="HAND PLACE")
    ]
    assert [n.text for n in res.notes] == [
        "note live",
        "note pcb",
        "heading note",
        "note hp",
    ]


def test_parse_section_9_errors_and_rendering(tmp_path):
    # 1. NO_HEADERED_SHEET
    p_no_hdr = tmp_path / "NOHDR AUDIT BOM.xlsx"
    wb = _make_workbook([
        ("Sheet1", True, [["Foo", "Bar"]]),
        ("HiddenSheet", False, [["Baz"]]),
    ])
    wb.save(str(p_no_hdr))
    wb.close()
    with pytest.raises(MalformedBomError) as exc:
        parse(p_no_hdr)
    assert exc.value.reason == "NO_HEADERED_SHEET"
    fp = render_error(exc.value)
    detail_dict = dict(fp.detail)
    assert "Sheet1" in detail_dict
    assert "HiddenSheet (hidden)" in detail_dict

    # 2. AMBIGUOUS_SHEET
    p_amb = tmp_path / "B123456 AUDIT BOM.xlsx"
    wb = _make_workbook([
        ("TabA", True, [CANONICAL_HEADER]),
        ("TabB", True, [CANONICAL_HEADER]),
    ])
    wb.save(str(p_amb))
    wb.close()
    with pytest.raises(MalformedBomError) as exc:
        parse(p_amb)
    assert exc.value.reason == "AMBIGUOUS_SHEET"
    fp_amb = render_error(exc.value)
    assert ("sheet", "TabA") in fp_amb.detail
    assert ("sheet", "TabB") in fp_amb.detail

    # 3. MISSING_COLUMN (singular and plural)
    p_miss = tmp_path / "MISS AUDIT BOM.xlsx"
    wb = _make_workbook([("AUDIT BOM", True, [["Find#", "PartNum", "Ref_Des", "SMT/THT"]])])
    wb.save(str(p_miss))
    wb.close()
    with pytest.raises(MalformedBomError) as exc:
        parse(p_miss)
    assert exc.value.reason == "MISSING_COLUMN"
    assert exc.value.detail["missing"] == ["Description"]
    fp_sing = render_error(exc.value)
    assert "no Description column" in fp_sing.summary
    fp_plur = render_error(MalformedBomError(
        p_miss,
        "MISSING_COLUMN",
        {"missing": ["Description", "SMT/THT"], "observed": ["Find#", "PartNum"]},
    ))
    assert "no Description, SMT/THT columns" in fp_plur.summary

    # 4. DUPLICATE_HEADER (singular and plural)
    p_dup_hdr = tmp_path / "DUPHDR AUDIT BOM.xlsx"
    wb = _make_workbook([("AUDIT BOM", True, [["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT", "PartNum"]])])
    wb.save(str(p_dup_hdr))
    wb.close()
    with pytest.raises(MalformedBomError) as exc:
        parse(p_dup_hdr)
    assert exc.value.reason == "DUPLICATE_HEADER"
    fp_dh_sing = render_error(exc.value)
    assert "PartNum appears more than once" in fp_dh_sing.summary
    fp_dh_plur = render_error(MalformedBomError(
        p_dup_hdr,
        "DUPLICATE_HEADER",
        {"duplicated": ["Find#", "PartNum"], "observed": []},
    ))
    assert "Find#, PartNum appear more than once" in fp_dh_plur.summary

    # 5. INVALID_REFDES_TOKEN
    p_ref = tmp_path / "BADREF AUDIT BOM.xlsx"
    wb = _make_workbook([("AUDIT BOM", True, [
        FIVE_CONSUMED_HEADER,
        [1, "MPN-1", "R1#BAD", "Res", "SMT"],
    ])])
    wb.save(str(p_ref))
    wb.close()
    with pytest.raises(MalformedBomError) as exc:
        parse(p_ref)
    assert exc.value.reason == "INVALID_REFDES_TOKEN"
    assert exc.value.detail["row"] == 2
    assert exc.value.detail["token"] == "R1#BAD"
    fp_ref = render_error(exc.value)
    assert "Row 2" in fp_ref.summary
    assert ("token", "R1#BAD") in fp_ref.detail

    # 6. MISSING_FIND_NUMBER
    p_mf = tmp_path / "MISSFIND AUDIT BOM.xlsx"
    wb = _make_workbook([("AUDIT BOM", True, [
        FIVE_CONSUMED_HEADER,
        ["", "MPN-1", "R1", "Res", "SMT"],
    ])])
    wb.save(str(p_mf))
    wb.close()
    with pytest.raises(MalformedBomError) as exc:
        parse(p_mf)
    assert exc.value.reason == "MISSING_FIND_NUMBER"
    assert exc.value.detail == {"row": 2, "mpn": "MPN-1"}
    fp_mf = render_error(exc.value)
    assert "Row 2" in fp_mf.summary
    assert ("PartNum", "MPN-1") in fp_mf.detail

    # 7. INVALID_FIND_NUMBER
    p_if = tmp_path / "INVFIND AUDIT BOM.xlsx"
    wb = _make_workbook([("AUDIT BOM", True, [
        FIVE_CONSUMED_HEADER,
        ["12A5", "MPN-1", "R1", "Res", "SMT"],
    ])])
    wb.save(str(p_if))
    wb.close()
    with pytest.raises(MalformedBomError) as exc:
        parse(p_if)
    assert exc.value.reason == "INVALID_FIND_NUMBER"
    assert exc.value.detail == {"row": 2, "mpn": "MPN-1", "raw": "12A5"}
    fp_if = render_error(exc.value)
    assert "Row 2" in fp_if.summary
    assert ("value", "12A5") in fp_if.detail

    # 8. DUPLICATE_MPN (singular and plural)
    p_dmpn = tmp_path / "DUPMPN AUDIT BOM.xlsx"
    wb = _make_workbook([("AUDIT BOM", True, [
        FIVE_CONSUMED_HEADER,
        [1, "MPN-DUP", "R1", "Res 1", "SMT"],
        [2, "MPN-OK", "C1", "Cap", "SMT"],
        [3, "MPN-DUP", "R2", "Res 2", "THT"],
    ])])
    wb.save(str(p_dmpn))
    wb.close()
    with pytest.raises(MalformedBomError) as exc:
        parse(p_dmpn)
    assert exc.value.reason == "DUPLICATE_MPN"
    assert exc.value.detail == {
        "duplicates": ["MPN-DUP"],
        "lines": {
            "MPN-DUP": [
                {"find_number": "1", "row": 2},
                {"find_number": "3", "row": 4},
            ]
        },
    }
    fp_dm_sing = render_error(exc.value)
    assert "1 part number appears on more than one line" in fp_dm_sing.summary
    assert ("MPN-DUP", "Find# 1 (row 2), Find# 3 (row 4)") in fp_dm_sing.detail

    fp_dm_plur = render_error(MalformedBomError(
        p_dmpn,
        "DUPLICATE_MPN",
        {
            "duplicates": ["A", "B"],
            "lines": {
                "A": [{"find_number": "1", "row": 2}],
                "B": [{"find_number": "2", "row": 3}],
            },
        },
    ))
    assert "2 part numbers appear on more than one line" in fp_dm_plur.summary


# ---- 9. build_cleaning_report & 10. describe_routine_cleaning --------------

def test_build_cleaning_report_ordering_and_rejected_rows(tmp_path):
    path = tmp_path / "B123456 AUDIT BOM.xlsx"
    wb = _make_workbook([
        ("Cover", True, [["Cover Page"]]),
        ("HiddenBOM", False, [
            FIVE_CONSUMED_HEADER,
            [1, '"MPN-1"', "R1 (note1)", "Res", "HAND PLACE"],
            [2, "MPN-2", "R2#BAD", "Res 2", "SMT"],
        ]),
    ])
    wb.save(str(path))
    wb.close()

    sheet = read_cleaned_sheet(path)
    report = build_cleaning_report(sheet, mount_gate_actions(sheet))
    assert report.rules_version == CLEANING_RULES_VERSION
    assert report.sheet_name == "HiddenBOM"
    assert report.sheet_is_visible is False
    # HiddenSheetRead is first; row 2's actions come before its gate action UnknownMountTypeSkipped
    assert isinstance(report.actions[0], HiddenSheetRead)
    assert isinstance(report.actions[1], EnclosingQuotesRemoved)
    assert isinstance(report.actions[2], NoteExtracted)
    assert isinstance(report.actions[3], UnknownMountTypeSkipped)
    assert len(report.rejected_rows) == 1
    assert report.rejected_rows[0].sheet_row == 3
    assert report.rejected_rows[0].token == "R2#BAD"


def test_describe_routine_cleaning_order_and_plurals():
    report = CleaningReport(
        rules_version=CLEANING_RULES_VERSION,
        sheet_name="AUDIT BOM",
        sheet_is_visible=True,
        header_row=1,
        actions=[
            DesignatorRepeatDropped(sheet_row=2, designator="C1"),
            StruckTextRemoved(sheet_row=2, column=ColumnName.DESCRIPTION),
            StruckTextRemoved(sheet_row=3, column=ColumnName.DESCRIPTION),
            EnclosingQuotesRemoved(sheet_row=2, column=ColumnName.PART_NUMBER),
            NoteExtracted(note=BomNote(2, "1", ColumnName.REF_DES, "note a")),
            NoteExtracted(note=BomNote(3, "2", ColumnName.REF_DES, "note b")),
        ],
        rejected_rows=[],
    )
    summary = summarise_cleaning(report)
    text = describe_routine_cleaning(summary)
    assert text == (
        "cleaned: 2 notes, 1 quoted value, 2 struck-through cells, 1 repeated designator"
    )


# ---- 11. read_raw_rows -----------------------------------------------------

def test_read_raw_rows_agrees_with_parse_and_tolerates_rejected_designator(tmp_path):
    path_ok = tmp_path / "B123456 AUDIT BOM.xlsx"
    wb = _make_workbook([
        ("AUDIT BOM", True, [
            FIVE_CONSUMED_HEADER,  # display columns absent!
            [1, '"MPN-1 *note1"', '"R1, R2 (top)"', '"Resistor"', '"SMT"'],
            [2, "MPN-STRUCK", "R9", "Removed", "SMT"],
        ]),
    ])
    wb["AUDIT BOM"].cell(row=3, column=2).font = Font(strike=True)
    wb.save(str(path_ok))
    wb.close()

    parsed = parse(path_ok)
    raw_rows = read_raw_rows(path_ok)
    # Row 3 was removed by strike, so only Row 2 remains
    assert len(raw_rows) == 1
    row0 = raw_rows[0]
    assert row0.sheet_ordinal == 2
    assert len(row0.cells) == 14
    assert row0.part_number == parsed.items[0].component_mpn == "MPN-1"
    assert row0.description == parsed.items[0].description == "Resistor"
    assert row0.designators == list(parsed.items[0].ref_des_list) == ["R1", "R2"]
    assert [n.text for n in row0.notes] == ["note1", "top"]

    # Tolerates a rejected designator without raising
    path_bad = tmp_path / "BAD AUDIT BOM.xlsx"
    wb_bad = _make_workbook([
        ("AUDIT BOM", True, [
            FIVE_CONSUMED_HEADER,
            [1, "MPN-1", "R1#BAD", "Desc", "SMT"],
        ]),
    ])
    wb_bad.save(str(path_bad))
    wb_bad.close()

    raw_bad = read_raw_rows(path_bad)
    assert len(raw_bad) == 1
    assert raw_bad[0].part_number == "MPN-1"
    assert raw_bad[0].designators == []
    assert raw_bad[0].cells[CANONICAL_COLUMNS.index("Ref_Des")] == "R1#BAD"


# ---- 12. Lock files --------------------------------------------------------

def test_lock_files_role_and_gatekeeper(tmp_path):
    assert role_of("~$x AUDIT BOM.xlsx") == SourceRole.UNKNOWN

    lock_path = tmp_path / "~$x AUDIT BOM.xlsx"
    t_path = tmp_path / "x TRAVELER.xlsx"
    e_path = tmp_path / "x ECO.docx"
    for p in (lock_path, t_path, e_path):
        p.write_bytes(b"data")

    with pytest.raises(GatekeeperViolation) as exc:
        gatekeeper_validate([lock_path, t_path, e_path])
    assert exc.value.reason == "LOCK_FILE"
    assert exc.value.detail == {"path": str(lock_path)}

    fp = render_error(exc.value)
    assert "lock file" in fp.summary
    assert ("path", str(lock_path)) in fp.detail


# ---- 13. Import guard ------------------------------------------------------

def test_audit_bom_refuses_to_import_without_cell_rich_text(monkeypatch):
    import importlib.util
    import openpyxl.cell.rich_text as rt_mod
    monkeypatch.setattr(rt_mod, "CellRichText", None)
    spec = importlib.util.spec_from_file_location(
        "cockpit.ingestion.parsers._audit_bom_guard_check",
        audit_bom_mod.__file__,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "cockpit.ingestion.parsers"
    with pytest.raises(ImportError, match="CellRichText"):
        spec.loader.exec_module(mod)
