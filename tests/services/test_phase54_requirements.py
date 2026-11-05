"""Phase 54 unit tests on synthetic documents (§15.1)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import zipfile

import docx
from docx.oxml import OxmlElement
import openpyxl
import pytest
from PyQt6.QtCore import QSettings

from cockpit.persistence.connection import open_connection
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.repositories.requirements import (
    AuditRequirementsRepository,
    SaveImagesNotRequired,
    set_xray_images_done,
)
from cockpit.persistence.schema import migrate
from cockpit.persistence.types import ActiveAuditDraft
from cockpit.services.requirements import (
    DEFAULT_VOCABULARY,
    DEFAULT_VOCABULARY_GENERATION,
    MAX_DOCX_ENTRY_BYTES,
    MAX_DOCX_TOTAL_BYTES,
    MAX_EVIDENCE,
    MAX_PATTERNS_PER_KIND,
    MAX_TERMS_PER_GROUP,
    MAX_TERM_CHARS,
    MAX_VOCABULARY_FILE_BYTES,
    NothingFollows,
    PlaceholderFollows,
    RequirementEvidence,
    RequirementKind,
    RequirementPattern,
    RequirementState,
    RequirementSummary,
    RequirementVocabulary,
    SINGLE_GROUP_WINDOW,
    ScanInputs,
    SectionAbsent,
    TableRows,
    TermGroup,
    TextUnit,
    UnitSource,
    Unrecognised,
    VOCABULARY_GENERATION_KEY,
    VOCABULARY_SETTINGS_KEY,
    XrayCellState,
    XrayTick,
    canonical_vocabulary,
    check_docx_package,
    classify_xray_section,
    decide_requirements,
    export_vocabulary_file,
    find_matches,
    import_vocabulary_file,
    is_dni_line,
    is_negated,
    load_vocabulary,
    merge_shipped_patterns,
    normalise,
    parse_negation_line,
    parse_pattern_line,
    read_bom_units,
    read_eco_units,
    render_negation_line,
    render_pattern_line,
    save_vocabulary_to_settings,
    scan_documents,
    scan_fingerprint,
    traveler_unit,
    xray_cell_state,
    xray_task_fingerprint,
)
from cockpit.services.requirements_reconciler import (
    PassSummary,
    RequirementsReconciler,
    ScanJob,
    pending_xray_warning,
    run_pass,
    stale_scan_jobs,
    write_scan_if_current,
)


# ---------------------------------------------------------------------------
# Helpers for synthetic .docx and .xlsx documents
# ---------------------------------------------------------------------------

def _make_docx(path: Path, paragraphs: list[str] = (), tables: list[list[list[str]]] = ()) -> Path:
    doc = docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    for tbl_data in tables:
        if not tbl_data:
            continue
        rows = len(tbl_data)
        cols = max(len(r) for r in tbl_data)
        tbl = doc.add_table(rows=rows, cols=cols)
        for r_idx, row in enumerate(tbl_data):
            for c_idx, val in enumerate(row):
                tbl.cell(r_idx, c_idx).text = val
    doc.save(str(path))
    return path


def _make_bom_xlsx(path: Path, rows: list[list[object]]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT"])
    for r in rows:
        ws.append(r)
    wb.save(str(path))
    wb.close()
    return path


# ---------------------------------------------------------------------------
# 1. normalise (§4.1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("x-ray", ["xray"]),
        ("x ray", ["xray"]),
        ("xray", ["xray"]),
        ("X\u2011Ray", ["xray"]),
        ("X\u2011Rays", ["xray"]),  # non-breaking hyphen U+2011 + plural
        ("x-rays", ["xray"]),
        ("x-rayed", ["xray"]),
        ("x-raying", ["xray"]),
        ("xrayed", ["xray"]),
        ("press-fit", ["pressfit"]),
        ("press fit", ["pressfit"]),
        ("pressfit", ["pressfit"]),
        ("press-fits", ["pressfit"]),
        ("press-fitted", ["pressfit"]),
        ("press-fitting", ["pressfit"]),
        ("pressed fit", ["pressfit"]),
        ("aquanox", ["aquanox"]),
        ("aqua-nox", ["aquanox"]),
        ("aqua nox", ["aquanox"]),
        ("n/a", ["na"]),
        ("N/A", ["na"]),
        ("don't", ["dont"]),
    ],
)
def test_normalise_every_compound_row(raw: str, expected: list[str]) -> None:
    tokens = normalise(raw)
    assert [t.text for t in tokens] == expected
    assert tokens[0].start == 0
    assert tokens[0].end == len(raw)


# ---------------------------------------------------------------------------
# 2. parse_pattern_line & render_pattern_line (§4.2, §10.1)
# ---------------------------------------------------------------------------

def test_parse_and_render_pattern_line() -> None:
    # Single group -> window stored as SINGLE_GROUP_WINDOW (0)
    res1 = parse_pattern_line("xray | x-ray")
    assert res1.is_ok
    assert res1.value.window_tokens == SINGLE_GROUP_WINDOW
    assert res1.value.groups == (TermGroup(frozenset({"xray"})),)
    assert render_pattern_line(res1.value) == "xray"
    assert parse_pattern_line(render_pattern_line(res1.value)).value == res1.value

    # Multi-group with window -> round trip
    res2 = parse_pattern_line("xray + save | take | capture + image | picture | photo ~ 8")
    assert res2.is_ok
    assert res2.value.window_tokens == 8
    rendered = render_pattern_line(res2.value)
    assert parse_pattern_line(rendered).value == res2.value

    # Window on a one-group line is refused
    err_one_win = parse_pattern_line("xray ~ 4")
    assert err_one_win.is_err

    # Missing window on a two-group pattern is refused
    err_no_win = parse_pattern_line("xray + save")
    assert err_no_win.is_err

    # Term that normalises to two tokens is refused
    err_two_tok = parse_pattern_line("save all + images ~ 4")
    assert err_two_tok.is_err

    # Window out of range (0 or 21) is refused
    assert parse_pattern_line("xray + save ~ 0").is_err
    assert parse_pattern_line("xray + save ~ 21").is_err
    assert parse_pattern_line("xray + save ~ 1").is_ok
    assert parse_pattern_line("xray + save ~ 20").is_ok

    # Exceeding MAX_TERMS_PER_GROUP (30)
    too_many_terms = " | ".join(f"t{i}" for i in range(MAX_TERMS_PER_GROUP + 1))
    assert parse_pattern_line(too_many_terms).is_err

    # Exceeding MAX_TERM_CHARS (40)
    long_term = "a" * (MAX_TERM_CHARS + 1)
    assert parse_pattern_line(long_term).is_err


# ---------------------------------------------------------------------------
# 3. find_matches & is_negated (§4.2, §4.3)
# ---------------------------------------------------------------------------

def test_find_matches_and_negation_table() -> None:
    xr_pat = parse_pattern_line("xray").value
    save_pat = parse_pattern_line("xray + save | take + image | images | pictures ~ 8").value
    solvent_pat = parse_pattern_line("solvent + wash | washing | washed ~ 2").value
    pf_pat = parse_pattern_line("pressfit").value

    # Every row of §4.3:
    # 1. "do not solvent wash" -> yes (`do not` sits directly before `solvent`)
    t1 = normalise("do not solvent wash")
    m1 = find_matches(t1, solvent_pat)
    assert len(m1) == 1
    assert is_negated(t1, m1[0][0], m1[0][1], DEFAULT_VOCABULARY) is True

    # 2. "Use No-Clean with Solvent Wash" -> no (the token before `solvent` is `with`)
    t2 = normalise("Use No-Clean with Solvent Wash")
    m2 = find_matches(t2, solvent_pat)
    assert len(m2) == 1
    assert is_negated(t2, m2[0][0], m2[0][1], DEFAULT_VOCABULARY) is False

    # 3. "NO-CLEAN/SOLVENT WASH" -> no (the token before `solvent` is `clean`)
    t3 = normalise("NO-CLEAN/SOLVENT WASH")
    m3 = find_matches(t3, solvent_pat)
    assert len(m3) == 1
    assert is_negated(t3, m3[0][0], m3[0][1], DEFAULT_VOCABULARY) is False

    # 4. "press-fit only - do not solder" -> no (the negation belongs to `solder`)
    t4 = normalise("press-fit only - do not solder")
    m4 = find_matches(t4, pf_pat)
    assert len(m4) == 1
    assert is_negated(t4, m4[0][0], m4[0][1], DEFAULT_VOCABULARY) is False

    # 5. "X-ray: N/A" -> yes (`na` sits directly after `xray`)
    t5 = normalise("X-ray: N/A")
    m5 = find_matches(t5, xr_pat)
    assert len(m5) == 1
    assert is_negated(t5, m5[0][0], m5[0][1], DEFAULT_VOCABULARY) is True

    # 6. "No x-ray & save images" -> yes (`no` sits directly before `xray`)
    t6 = normalise("No x-ray & save images")
    m6 = find_matches(t6, save_pat)
    assert len(m6) == 1
    assert is_negated(t6, m6[0][0], m6[0][1], DEFAULT_VOCABULARY) is True


def test_find_matches_window_boundaries_and_ordering() -> None:
    pat8 = parse_pattern_line("xray + save + image ~ 8").value
    # Spread (last - first) == 8 (indices 0..8 -> 9 tokens) matches
    toks_spread_8 = normalise("xray w1 w2 w3 w4 w5 w6 save image")
    assert toks_spread_8[-1].text == "image"
    assert len(toks_spread_8) - 1 == 8
    assert find_matches(toks_spread_8, pat8) == [(0, 8)]

    # Spread (last - first) == 9 (indices 0..9 -> 10 tokens) does NOT match
    toks_spread_9 = normalise("xray w1 w2 w3 w4 w5 w6 w7 save image")
    assert len(toks_spread_9) - 1 == 9
    assert find_matches(toks_spread_9, pat8) == []

    # ~ 1 matches only adjacent words (last - first == 1)
    pat_adj = parse_pattern_line("solvent + wash ~ 1").value
    assert find_matches(normalise("solvent wash"), pat_adj) == [(0, 1)]
    assert find_matches(normalise("solvent spray wash"), pat_adj) == []

    # Single-group pattern matches each occurrence
    pat_single = parse_pattern_line("xray").value
    toks_multi = normalise("xray U1 and then xray U2")
    assert find_matches(toks_multi, pat_single) == [(0, 0), (4, 4)]

    # Shortest-then-leftmost span selection
    pat_pair = parse_pattern_line("alpha + beta ~ 6").value
    toks_overlap = normalise("alpha one two alpha beta")
    assert find_matches(toks_overlap, pat_pair) == [(3, 4)]


# ---------------------------------------------------------------------------
# 4. read_eco_units & classify_xray_section (§3.1)
# ---------------------------------------------------------------------------

def test_read_eco_units_sources_exclusions_and_xray_section(tmp_path: Path) -> None:
    doc = docx.Document()
    # 1. Paragraph with live text + struck run + tracked deletion + tracked insertion + hyperlink + textbox
    p = doc.add_paragraph()
    p.add_run("Live paragraph text ")
    r_struck = p.add_run("struck text ")
    r_struck.font.strike = True

    # Tracked deletion <w:del><w:r><w:delText>deleted text</w:delText></w:r></w:del>
    w_del = OxmlElement("w:del")
    w_del_r = OxmlElement("w:r")
    w_del_t = OxmlElement("w:delText")
    w_del_t.text = "deleted text "
    w_del_r.append(w_del_t)
    w_del.append(w_del_r)
    p._p.append(w_del)

    # Tracked insertion <w:ins><w:r><w:t>inserted aquanox</w:t></w:r></w:ins>
    w_ins = OxmlElement("w:ins")
    w_ins_r = OxmlElement("w:r")
    w_ins_t = OxmlElement("w:t")
    w_ins_t.text = "inserted aquanox "
    w_ins_r.append(w_ins_t)
    w_ins.append(w_ins_r)
    p._p.append(w_ins)

    # Hyperlink <w:hyperlink><w:r><w:t>linked press-fit</w:t></w:r></w:hyperlink>
    w_hl = OxmlElement("w:hyperlink")
    w_hl_r = OxmlElement("w:r")
    w_hl_t = OxmlElement("w:t")
    w_hl_t.text = "linked press-fit"
    w_hl_r.append(w_hl_t)
    w_hl.append(w_hl_r)
    p._p.append(w_hl)

    # Text box <w:r><w:txbxContent><w:p><w:r><w:t>textbox xray save image</w:t></w:r></w:p></w:txbxContent></w:r>
    w_tb_r = OxmlElement("w:r")
    w_txbx = OxmlElement("w:txbxContent")
    w_tb_p = OxmlElement("w:p")
    w_tb_pr = OxmlElement("w:r")
    w_tb_pt = OxmlElement("w:t")
    w_tb_pt.text = "textbox xray save image"
    w_tb_pr.append(w_tb_pt)
    w_tb_p.append(w_tb_pr)
    w_txbx.append(w_tb_p)
    w_tb_r.append(w_txbx)
    p._p.append(w_tb_r)

    # 2. Normal table with nested table in cell (0, 0)
    tbl = doc.add_table(rows=1, cols=2)
    tbl.cell(0, 0).text = "Outer cell"
    nested = tbl.cell(0, 0).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "Nested cell text"
    tbl.cell(0, 1).text = "Second cell"

    # 3. X-ray section marker followed by X-ray table with:
    #    - valid row ("U1, U2")
    #    - TokenRejected row ("U 99")
    #    - fully struck row (excluded from live_rows)
    doc.add_paragraph("Please X-ray the following locations:")
    xtbl = doc.add_table(rows=4, cols=3)
    xtbl.cell(0, 0).text = "Find#"
    xtbl.cell(0, 1).text = "PartNum"
    xtbl.cell(0, 2).text = "Ref_Des"

    xtbl.cell(1, 0).text = "10"
    xtbl.cell(1, 1).text = "BGA-1"
    xtbl.cell(1, 2).text = "U1, U2"

    xtbl.cell(2, 0).text = "20"
    xtbl.cell(2, 1).text = "BGA-2"
    xtbl.cell(2, 2).text = "U 99"  # TokenRejected -> contributes no locations

    # Row 3: fully struck
    for col_i, txt in enumerate(["30", "BGA-3", "U3"]):
        cell_p = xtbl.cell(3, col_i).paragraphs[0]
        cell_p.text = ""
        sr = cell_p.add_run(txt)
        sr.font.strike = True

    docx_path = tmp_path / "eco.docx"
    doc.save(str(docx_path))

    res = read_eco_units(docx_path, DEFAULT_VOCABULARY)
    assert res.is_ok
    eco = res.value

    all_texts = [u.text for u in eco.units]
    joined = "\n".join(all_texts)
    assert "Live paragraph text" in joined
    assert "inserted aquanox" in joined
    assert "linked press-fit" in joined
    assert "textbox xray save image" in joined
    assert "Outer cell\nNested cell text" in all_texts
    assert "struck text" not in joined
    assert "deleted text" not in joined

    # X-ray table had 2 live rows (row 1 and row 2; row 3 was fully struck)
    assert eco.xray_section == TableRows(live_rows=2)
    assert set(eco.xray_locations) == {"U1", "U2"}


@pytest.mark.parametrize(
    "following_kind,expected_type",
    [
        ("na", PlaceholderFollows),
        ("dash", PlaceholderFollows),
        ("nothing", NothingFollows),
        ("image", Unrecognised),
        ("other_text", Unrecognised),
    ],
)
def test_classify_xray_section_variants(tmp_path: Path, following_kind: str, expected_type: type) -> None:
    doc = docx.Document()
    doc.add_paragraph("Please X-ray the following locations:")
    if following_kind == "na":
        doc.add_paragraph("N/A")
    elif following_kind == "dash":
        doc.add_paragraph("-")
    elif following_kind == "nothing":
        pass
    elif following_kind == "image":
        p = doc.add_paragraph()
        r = OxmlElement("w:r")
        drawing = OxmlElement("w:drawing")
        r.append(drawing)
        p._p.append(r)
    elif following_kind == "other_text":
        doc.add_paragraph("See customer portal for details")

    p_docx = tmp_path / f"eco_{following_kind}.docx"
    doc.save(str(p_docx))

    res = read_eco_units(p_docx, DEFAULT_VOCABULARY)
    assert res.is_ok
    assert isinstance(res.value.xray_section, expected_type)


# ---------------------------------------------------------------------------
# 5. check_docx_package & forged zip entry (§3.1, §15.1)
# ---------------------------------------------------------------------------

def test_check_docx_package_caps_and_forged_entry(tmp_path: Path) -> None:
    valid_path = _make_docx(tmp_path / "valid.docx", ["Hello"])
    assert check_docx_package(valid_path).is_ok

    # 1. Single entry declared above MAX_DOCX_ENTRY_BYTES, refused before python-docx opens anything
    huge_entry_zip = tmp_path / "huge_entry.docx"
    with zipfile.ZipFile(huge_entry_zip, "w") as zf:
        zf.writestr("word/document.xml", b"<w:document/>")
    raw = bytearray(huge_entry_zip.read_bytes())
    cd_pos = raw.find(b"PK\x01\x02")
    assert cd_pos != -1
    struct.pack_into("<I", raw, cd_pos + 24, MAX_DOCX_ENTRY_BYTES + 100)
    huge_entry_zip.write_bytes(bytes(raw))

    res_entry = check_docx_package(huge_entry_zip)
    assert res_entry.is_err
    assert "MAX_DOCX_ENTRY_BYTES" in res_entry.error.reason

    # 2. Entries summing above MAX_DOCX_TOTAL_BYTES (including image parts)
    huge_total_zip = tmp_path / "huge_total.docx"
    with zipfile.ZipFile(huge_total_zip, "w") as zf:
        zf.writestr("word/document.xml", b"<w:document/>")
        zf.writestr("word/media/image1.png", b"1234")
        zf.writestr("word/media/image2.png", b"5678")
        zf.writestr("word/media/image3.png", b"9012")
        zf.writestr("word/media/image4.png", b"3456")
    raw_tot = bytearray(huge_total_zip.read_bytes())
    pos = 0
    while True:
        idx = raw_tot.find(b"PK\x01\x02", pos)
        if idx == -1:
            break
        struct.pack_into("<I", raw_tot, idx + 24, MAX_DOCX_ENTRY_BYTES - 1024)
        pos = idx + 46
    huge_total_zip.write_bytes(bytes(raw_tot))

    res_total = check_docx_package(huge_total_zip)
    assert res_total.is_err
    assert "MAX_DOCX_TOTAL_BYTES" in res_total.error.reason

    # 3. Understated declared size / CRC mismatch fails in zipfile/read_eco_units as DocumentUnreadable
    understated_zip = tmp_path / "understated.docx"
    shutil_bytes = bytearray(valid_path.read_bytes())
    lf_pos = shutil_bytes.find(b"PK\x03\x04")
    struct.pack_into("<I", shutil_bytes, lf_pos + 14, 0xDEADBEEF)
    cd_pos2 = shutil_bytes.find(b"PK\x01\x02")
    struct.pack_into("<I", shutil_bytes, cd_pos2 + 16, 0xDEADBEEF)
    understated_zip.write_bytes(bytes(shutil_bytes))

    res_under = read_eco_units(understated_zip)
    assert res_under.is_err


# ---------------------------------------------------------------------------
# 6. Entity safety guard (§3.1, §15.1)
# ---------------------------------------------------------------------------

def test_docx_entity_safety_guard(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("AQUANOX PRESSFIT SECRET", encoding="utf-8")

    base_docx = _make_docx(tmp_path / "base.docx", ["Safe text only"])
    entity_docx = tmp_path / "entity.docx"

    xml_with_entities = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<!DOCTYPE w:document [
  <!ENTITY internal "AQUANOX">
  <!ENTITY ext SYSTEM "{secret_file.as_uri()}">
  <!ENTITY a "x">
  <!ENTITY b "&a;&a;&a;&a;">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Safe &internal; &ext; &b;</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(base_docx, "r") as zin, zipfile.ZipFile(entity_docx, "w") as zout:
        for item in zin.infolist():
            if item.filename == "word/document.xml":
                zout.writestr(item, xml_with_entities.encode("utf-8"))
            else:
                zout.writestr(item, zin.read(item.filename))

    res = read_eco_units(entity_docx, DEFAULT_VOCABULARY)
    if res.is_ok:
        texts = " ".join(u.text for u in res.value.units)
        assert "AQUANOX" not in texts
        assert "SECRET" not in texts


# ---------------------------------------------------------------------------
# 7. read_bom_units & is_dni_line (§3.2, §14.1)
# ---------------------------------------------------------------------------

def test_read_bom_units_and_is_dni_line(tmp_path: Path) -> None:
    bom_path = tmp_path / "test_bom.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT"])
    # Row 2: normal line with paired note and description
    ws.append([1, "MPN-1", "J1, J2 (press-fit connector)", "CONN HEADER", "THT"])
    # Row 3: DNI by PartNum
    ws.append([2, "DNI", "U10 (please x-ray)", "IC BGA PRESS-FIT", "SMT"])
    # Row 4: DNI by SMT/THT
    ws.append([3, "MPN-3", "U11 (please x-ray)", "IC BGA PRESS-FIT", "DNI"])
    # Row 5: PCB line (must be included!)
    ws.append([4, "PCB", "PCB1 (wash in aquanox)", "BARE BOARD", "PCB"])
    # Row 6: DesignatorRejected row ("D 19") with paired note
    ws.append([5, "MPN-5", "D6, D 19 *Please X-ray*", "DIODE", "SMT"])
    # Row 7: Fully struck line (removed)
    ws.append([6, "MPN-6", "U99 (please x-ray)", "STRUCK PART", "SMT"])
    for cell in ws[7]:
        cell.font = openpyxl.styles.Font(strike=True)

    wb.save(str(bom_path))
    wb.close()

    res = read_bom_units(bom_path)
    assert res.is_ok
    units = res.value

    excerpts = [u.text for u in units]
    assert any("press-fit connector" in t for t in excerpts)
    assert any("wash in aquanox" in t for t in excerpts)
    assert any("Please X-ray" in t for t in excerpts)
    assert not any("U10" in u.location for u in units)
    assert not any("U11" in u.location for u in units)
    assert not any("STRUCK PART" in t for t in excerpts)

    # Check designators on J1, J2 note vs DesignatorRejected note
    j_notes = [u for u in units if "press-fit connector" in u.text]
    assert len(j_notes) == 1
    assert set(j_notes[0].designators) == {"J1", "J2"}

    rej_notes = [u for u in units if "Please X-ray" in u.text]
    assert len(rej_notes) == 1
    assert set(rej_notes[0].designators) == set()


# ---------------------------------------------------------------------------
# 8. decide_requirements (§4.5, §4.6)
# ---------------------------------------------------------------------------

def test_decide_requirements_matrix_and_rules(tmp_path: Path) -> None:
    # Unreadable ECO + BOM X-ray note -> XR DETECTED, XRAY_SAVE_IMAGES UNKNOWN
    bom_with_xr = _make_bom_xlsx(
        tmp_path / "xr_bom.xlsx",
        [[1, "MPN-1", "U1 (x-ray required)", "BGA", "SMT"]],
    )
    scan1 = scan_documents(
        ScanInputs(
            notes_path=tmp_path / "missing.docx",
            notes_hash="h1",
            bom_path=bom_with_xr,
            bom_hash="h2",
            traveler_wash_text="NC",
            vocabulary=DEFAULT_VOCABULARY,
        )
    )
    assert scan1.states[RequirementKind.XRAY] == RequirementState.DETECTED
    assert scan1.states[RequirementKind.XRAY_SAVE_IMAGES] == RequirementState.UNKNOWN

    # Unreadable ECO + no BOM X-ray note -> XR UNKNOWN
    bom_plain = _make_bom_xlsx(
        tmp_path / "plain_bom.xlsx",
        [[1, "MPN-1", "R1", "RES 10K", "SMT"]],
    )
    scan2 = scan_documents(
        ScanInputs(
            notes_path=tmp_path / "missing.docx",
            notes_hash="h1",
            bom_path=bom_plain,
            bom_hash="h2",
            traveler_wash_text="NC",
            vocabulary=DEFAULT_VOCABULARY,
        )
    )
    assert scan2.states[RequirementKind.XRAY] == RequirementState.UNKNOWN

    # Marker followed by N/A -> XR ABSENT (marker never raises XR on its own)
    eco_na = _make_docx(tmp_path / "eco_na.docx", ["Please X-ray the following locations:", "N/A"])
    scan3 = scan_documents(
        ScanInputs(
            notes_path=eco_na,
            notes_hash="h1",
            bom_path=bom_plain,
            bom_hash="h2",
            traveler_wash_text="NC",
            vocabulary=DEFAULT_VOCABULARY,
        )
    )
    assert scan3.states[RequirementKind.XRAY] == RequirementState.ABSENT

    # Evidence capped at MAX_EVIDENCE while xray_task still sees all matches
    many_paragraphs = [f"Step {i}: X-ray and save images of U{i}" for i in range(MAX_EVIDENCE + 5)]
    eco_many = _make_docx(tmp_path / "eco_many.docx", many_paragraphs)
    scan_many = scan_documents(
        ScanInputs(
            notes_path=eco_many,
            notes_hash="h1",
            bom_path=bom_plain,
            bom_hash="h2",
            traveler_wash_text="NC",
            vocabulary=DEFAULT_VOCABULARY,
        )
    )
    assert len(scan_many.evidence) == MAX_EVIDENCE

    # Changing the last paragraph (beyond MAX_EVIDENCE) still changes xray_task!
    many_paragraphs_modified = list(many_paragraphs)
    many_paragraphs_modified[-1] = "Step 99: X-ray and save images of U999"
    eco_many_mod = _make_docx(tmp_path / "eco_many_mod.docx", many_paragraphs_modified)
    scan_many_mod = scan_documents(
        ScanInputs(
            notes_path=eco_many_mod,
            notes_hash="h1",
            bom_path=bom_plain,
            bom_hash="h2",
            traveler_wash_text="NC",
            vocabulary=DEFAULT_VOCABULARY,
        )
    )
    assert scan_many.xray_task != scan_many_mod.xray_task


# ---------------------------------------------------------------------------
# 9. Fingerprints (§6.1, §6.2)
# ---------------------------------------------------------------------------

def test_fingerprints_scope_rewording_and_canonical_order(tmp_path: Path) -> None:
    eco_a = _make_docx(
        tmp_path / "eco_a.docx",
        ["X-ray and save images for U1"],
        [[["Find#", "Ref_Des"], ["1", "U1"]]],
    )
    eco_b = _make_docx(
        tmp_path / "eco_b.docx",
        ["X-ray and save images for U1 and U2"],  # Scope change in ECO unit!
        [[["Find#", "Ref_Des"], ["1", "U1"]]],
    )
    bom_1 = _make_bom_xlsx(
        tmp_path / "bom_1.xlsx",
        [[1, "MPN-1", "U5 (please x-ray)", "BGA", "SMT"]],
    )
    bom_2 = _make_bom_xlsx(
        tmp_path / "bom_2.xlsx",
        [[1, "MPN-1", "U5 (kindly x-ray this part)", "BGA", "SMT"]],  # Reworded BOM note, same designator U5
    )
    bom_3 = _make_bom_xlsx(
        tmp_path / "bom_3.xlsx",
        [[1, "MPN-1", "U5, U6 (please x-ray)", "BGA", "SMT"]],  # Added designator U6
    )

    s_a1 = scan_documents(ScanInputs(eco_a, "h1", bom_1, "b1", None, DEFAULT_VOCABULARY))
    s_b1 = scan_documents(ScanInputs(eco_b, "h2", bom_1, "b1", None, DEFAULT_VOCABULARY))
    s_a2 = scan_documents(ScanInputs(eco_a, "h1", bom_2, "b2", None, DEFAULT_VOCABULARY))
    s_a3 = scan_documents(ScanInputs(eco_a, "h1", bom_3, "b3", None, DEFAULT_VOCABULARY))

    # ECO scope change changes xray_task
    assert s_a1.xray_task != s_b1.xray_task
    # BOM note rewording with same designator U5 keeps xray_task identical
    assert s_a1.xray_task == s_a2.xray_task
    # BOM note adding designator U6 changes xray_task
    assert s_a1.xray_task != s_a3.xray_task

    # Canonical vocabulary is independent of pattern order, duplicates, and generation
    p1 = parse_pattern_line("xray").value
    p2 = parse_pattern_line("aquanox").value
    v_order1 = RequirementVocabulary(
        patterns={RequirementKind.XRAY: (p1, p2, p1)},
        xray_section_markers=DEFAULT_VOCABULARY.xray_section_markers,
        admitted_sources=DEFAULT_VOCABULARY.admitted_sources,
        negations_before=(("no",), ("not",), ("no",)),
        negations_after=(("not", "needed"),),
        generation=1,
    )
    v_order2 = RequirementVocabulary(
        patterns={RequirementKind.XRAY: (p2, p1)},
        xray_section_markers=DEFAULT_VOCABULARY.xray_section_markers,
        admitted_sources=DEFAULT_VOCABULARY.admitted_sources,
        negations_before=(("not",), ("no",)),
        negations_after=(("not", "needed"),),
        generation=99,
    )
    assert canonical_vocabulary(v_order1) == canonical_vocabulary(v_order2)
    inp1 = ScanInputs(eco_a, "h1", bom_1, "b1", "C", v_order1)
    inp2 = ScanInputs(eco_a, "h1", bom_1, "b1", "C", v_order2)
    assert scan_fingerprint(inp1) == scan_fingerprint(inp2)


# ---------------------------------------------------------------------------
# 10. xray_cell_state (§8)
# ---------------------------------------------------------------------------

def test_xray_cell_state_every_branch() -> None:
    now = datetime.now(timezone.utc)
    # 1. NOT_SCANNED
    assert xray_cell_state(None, XrayTick()) == XrayCellState.NOT_SCANNED

    def _sum(save_st: RequirementState, xr_st: RequirementState, task: str | None = "t1") -> RequirementSummary:
        return RequirementSummary(
            states={
                RequirementKind.XRAY_SAVE_IMAGES: save_st,
                RequirementKind.XRAY: xr_st,
                RequirementKind.PRESS_FIT: RequirementState.ABSENT,
                RequirementKind.AQUANOX: RequirementState.ABSENT,
            },
            unknown_reasons={},
            xray_task=task,
            fingerprint="fp",
            scanned_at=now,
        )

    # 2. Save-images UNKNOWN -> UNKNOWN (even if XR is DETECTED)
    assert xray_cell_state(_sum(RequirementState.UNKNOWN, RequirementState.DETECTED), XrayTick()) == XrayCellState.UNKNOWN
    # 3. Save-images DETECTED, unticked -> PENDING
    assert xray_cell_state(_sum(RequirementState.DETECTED, RequirementState.DETECTED, "t1"), XrayTick(False, None, None)) == XrayCellState.PENDING
    # 4. Save-images DETECTED, ticked with matching task -> DONE
    assert xray_cell_state(_sum(RequirementState.DETECTED, RequirementState.DETECTED, "t1"), XrayTick(True, "t1", now)) == XrayCellState.DONE
    # 5. Save-images DETECTED, ticked with older task -> DONE_STALE
    assert xray_cell_state(_sum(RequirementState.DETECTED, RequirementState.DETECTED, "t2"), XrayTick(True, "t1", now)) == XrayCellState.DONE_STALE
    # 6. Save-images ABSENT, XR DETECTED -> XR_ONLY
    assert xray_cell_state(_sum(RequirementState.ABSENT, RequirementState.DETECTED, None), XrayTick()) == XrayCellState.XR_ONLY
    # 7. Save-images ABSENT, XR UNKNOWN -> UNKNOWN
    assert xray_cell_state(_sum(RequirementState.ABSENT, RequirementState.UNKNOWN, None), XrayTick()) == XrayCellState.UNKNOWN
    # 8. Save-images ABSENT, XR ABSENT -> BLANK
    assert xray_cell_state(_sum(RequirementState.ABSENT, RequirementState.ABSENT, None), XrayTick()) == XrayCellState.BLANK


# ---------------------------------------------------------------------------
# 11. Vocabulary load, merge, export, import (§10.2)
# ---------------------------------------------------------------------------

def test_load_and_merge_vocabulary_and_import_export(tmp_path: Path) -> None:
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)

    # Absent key -> returns DEFAULT_VOCABULARY and writes nothing
    outcome0 = load_vocabulary(settings)
    assert outcome0.vocabulary == DEFAULT_VOCABULARY
    assert outcome0.added == []
    assert not settings.contains(VOCABULARY_SETTINGS_KEY)

    # Deleted pattern from generation 1 stays deleted; new pattern from generation 2 is added;
    # same-groups pattern with different window is not duplicated; generation never moves backwards.
    stored_without_xray = RequirementVocabulary(
        patterns={
            RequirementKind.XRAY_SAVE_IMAGES: DEFAULT_VOCABULARY.patterns[RequirementKind.XRAY_SAVE_IMAGES],
            RequirementKind.XRAY: (),  # operator deleted "xray"
            RequirementKind.PRESS_FIT: DEFAULT_VOCABULARY.patterns[RequirementKind.PRESS_FIT],
            RequirementKind.AQUANOX: (parse_pattern_line("solvent + wash | washing | washed ~ 5").value,),  # same groups, window 5
        },
        xray_section_markers=DEFAULT_VOCABULARY.xray_section_markers,
        admitted_sources=DEFAULT_VOCABULARY.admitted_sources,
        negations_before=DEFAULT_VOCABULARY.negations_before,
        negations_after=DEFAULT_VOCABULARY.negations_after,
        generation=1,
    )
    gen2_new_pat = parse_pattern_line("kyzen").value
    gen2_vocab = RequirementVocabulary(
        patterns={
            **DEFAULT_VOCABULARY.patterns,
            RequirementKind.AQUANOX: DEFAULT_VOCABULARY.patterns[RequirementKind.AQUANOX] + (gen2_new_pat,),
        },
        xray_section_markers=DEFAULT_VOCABULARY.xray_section_markers,
        admitted_sources=DEFAULT_VOCABULARY.admitted_sources,
        negations_before=DEFAULT_VOCABULARY.negations_before,
        negations_after=DEFAULT_VOCABULARY.negations_after,
        generation=2,
    )
    merged = merge_shipped_patterns(
        stored_without_xray,
        stored_generation=1,
        shipped_by_generation={1: DEFAULT_VOCABULARY, 2: gen2_vocab},
    )
    # "xray" (from gen 1) was not re-added
    assert merged.vocabulary.patterns[RequirementKind.XRAY] == ()
    # "kyzen" (from gen 2) was added
    aq_rendered = [render_pattern_line(p) for p in merged.vocabulary.patterns[RequirementKind.AQUANOX]]
    assert "kyzen" in aq_rendered
    # "solvent + wash | washing | washed ~ 2" was NOT added because window 5 has the same groups
    assert aq_rendered.count("solvent + wash | washed | washing ~ 5") == 1
    assert "solvent + wash | washed | washing ~ 2" not in aq_rendered
    assert merged.generation_advanced_to == 2

    # Higher generation never moves backwards
    higher_gen = merge_shipped_patterns(
        DEFAULT_VOCABULARY,
        stored_generation=7,
        shipped_by_generation={1: DEFAULT_VOCABULARY},
    )
    assert higher_gen.generation_advanced_to == 7
    assert higher_gen.vocabulary.generation == 7

    # Export and import round-trip (keeping higher generation)
    exp_path = tmp_path / "vocab.json"
    export_vocabulary_file(exp_path, higher_gen.vocabulary)
    imp_res = import_vocabulary_file(exp_path)
    assert imp_res.is_ok
    assert imp_res.value == higher_gen.vocabulary
    assert imp_res.value.generation == 7

    # Import size cap
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_VOCABULARY_FILE_BYTES + 1))
    assert import_vocabulary_file(oversized).is_err

    # Unknown version / unknown kind -> refused with "made by a newer Cockpit; update this PC first"
    bad_ver = tmp_path / "bad_ver.json"
    bad_ver.write_text(json.dumps({"format": "cockpit.requirements.vocabulary", "version": 99}), encoding="utf-8")
    res_ver = import_vocabulary_file(bad_ver)
    assert res_ver.is_err
    assert "made by a newer Cockpit; update this PC first" in res_ver.error

    bad_kind = tmp_path / "bad_kind.json"
    data_kind = json.loads(exp_path.read_text(encoding="utf-8"))
    data_kind["patterns"]["LASER_ETCH"] = ["laser"]
    bad_kind.write_text(json.dumps(data_kind), encoding="utf-8")
    res_kind = import_vocabulary_file(bad_kind)
    assert res_kind.is_err
    assert "made by a newer Cockpit; update this PC first" in res_kind.error


# ---------------------------------------------------------------------------
# 12. Reconciler, compare-and-set, split tick reset, and shutdown (§5.3, §7.2, §7.3)
# ---------------------------------------------------------------------------

def test_reconciler_cas_split_and_shutdown(tmp_path: Path, qtbot) -> None:
    conn = open_connection(tmp_path / "test.db")
    migrate(conn, parser_registry=None)

    bom_repo = AuditBomComponentRepository(conn)
    pdf_repo = PdfComponentCoordRepository(conn)
    audit_repo = AuditRepository(conn, bom_repo, pdf_repo)
    req_repo = AuditRequirementsRepository(conn)

    eco_path = _make_docx(
        tmp_path / "eco.docx",
        ["Please X-ray and save images for customer"],
        [[["Find#", "Ref_Des"], ["1", "U1"]]],
    )
    bom_path = _make_bom_xlsx(
        tmp_path / "bom.xlsx",
        [[1, "MPN-1", "U1 (x-ray)", "BGA", "SMT"]],
    )

    audit = audit_repo.create(
        ActiveAuditDraft(
            part_number="ASSY-1",
            work_order_ref="WO-100",
            quantity=10,
            traveler_metadata={"process_clean": "NC"},
        )
    )
    conn.execute(
        "INSERT INTO source_files (audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (?, 'Notes', 'eco.docx', ?, 'h_eco', '2026-01-01T00:00:00'), "
        "       (?, 'BOM', 'bom.xlsx', ?, 'h_bom', '2026-01-01T00:00:00')",
        (audit.id, str(eco_path), audit.id, str(bom_path)),
    )
    conn.commit()

    # 1. stale_scan_jobs lists the unscanned audit
    jobs = stale_scan_jobs(conn, DEFAULT_VOCABULARY)
    assert len(jobs) == 1
    assert jobs[0].audit_id == audit.id

    # 2. Compare-and-set refuses a scan whose fingerprint no longer matches current DB inputs
    scan = scan_documents(jobs[0].inputs)
    conn.execute("UPDATE source_files SET file_hash = 'changed_hash' WHERE audit_id = ? AND file_category = 'Notes'", (audit.id,))
    conn.commit()
    assert write_scan_if_current(conn, audit.id, scan, DEFAULT_VOCABULARY) is False

    # Restore hash -> run_pass writes it, and the summary counts one changed state
    conn.execute("UPDATE source_files SET file_hash = 'h_eco' WHERE audit_id = ? AND file_category = 'Notes'", (audit.id,))
    conn.commit()
    summary = run_pass(conn, stale_scan_jobs(conn, DEFAULT_VOCABULARY), DEFAULT_VOCABULARY)
    assert summary == PassSummary(
        jobs=1, written=1, dropped_as_stale=0, changed_state=1, failed=0, stopped_early=False,
    )
    assert stale_scan_jobs(conn, DEFAULT_VOCABULARY) == []

    # 3. Tick X-ray & save images, then clone_to_suffix -> sibling gets the scan but starts unticked!
    tick_res = set_xray_images_done(conn, audit.id, True)
    assert tick_res.is_ok
    ticked_audit = audit_repo.find_by_id(audit.id)
    assert ticked_audit.xray_images_done is True
    assert ticked_audit.xray_images_done_task == scan.xray_task
    assert ticked_audit.xray_images_done_at is not None

    sibling = audit_repo.clone_to_suffix(audit.id, "-B", 5, "split")
    assert sibling.xray_images_done is False
    assert sibling.xray_images_done_task is None
    assert sibling.xray_images_done_at is None
    sibling_summary = req_repo.find_summary(sibling.id)
    assert sibling_summary is not None
    assert sibling_summary.fingerprint == scan.fingerprint

    # 4. Reconciler shutdown contract (signals disconnected, bounded wait)
    reconciler = RequirementsReconciler(conn, lambda: DEFAULT_VOCABULARY)
    assert reconciler.shutdown(timeout_ms=100) is True

    conn.close()
