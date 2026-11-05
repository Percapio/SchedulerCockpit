"""Phase 54 local corpus regression and sample acceptance tests (§15.3, §15.4).

Skipped automatically when backend/data is absent (e.g. on CI).
"""

from __future__ import annotations

import json
from pathlib import Path

import docx
import openpyxl
import pytest

from cockpit.ingestion.hashing import sha256_hex
from cockpit.persistence.repositories.requirements import set_xray_images_done
from cockpit.persistence.traveler_flags import WashState, wash_state
from cockpit.services.requirements import (
    DEFAULT_VOCABULARY,
    RequirementKind,
    RequirementState,
    ScanInputs,
    TableRows,
    UnitSource,
    XrayCellState,
    decide_requirements,
    find_matches,
    is_negated,
    normalise,
    read_bom_units,
    read_eco_units,
    scan_documents,
    xray_cell_state,
)
from cockpit.services.requirements_reconciler import pending_xray_warning, run_pass, stale_scan_jobs
from cockpit.ui.bootstrap import bootstrap
from cockpit.ui.config import resolve_config

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "backend" / "data"
CORPUS_DIR = DATA_DIR / "data"
EXPECTATIONS_PATH = DATA_DIR / "phase54_expectations.json"


@pytest.mark.skipif(
    not EXPECTATIONS_PATH.is_file(),
    reason="Local expectations file backend/data/phase54_expectations.json not present",
)
def test_sample_ecos_match_phase54_expectations(tmp_path: Path) -> None:
    expectations = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))["sample_eco_expectations"]
    empty_bom = tmp_path / "empty_bom.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT"])
    wb.save(str(empty_bom))
    wb.close()

    for rel_path, exp in expectations.items():
        eco_path = DATA_DIR / rel_path
        if not eco_path.is_file():
            pytest.skip(f"Sample ECO {rel_path} not present")
        eco_res = read_eco_units(eco_path, DEFAULT_VOCABULARY)
        assert eco_res.is_ok, f"{rel_path} failed: {eco_res}"
        sec = eco_res.value.xray_section
        assert type(sec).__name__ == exp["xray_section"], f"{rel_path}: {sec}"
        if isinstance(sec, TableRows) and "live_rows" in exp:
            assert sec.live_rows == exp["live_rows"], f"{rel_path}: {sec}"

        scan = scan_documents(
            ScanInputs(
                notes_path=eco_path,
                notes_hash="h_eco",
                bom_path=empty_bom,
                bom_hash="h_bom",
                traveler_wash_text=None,
                vocabulary=DEFAULT_VOCABULARY,
            )
        )
        assert scan.states[RequirementKind.XRAY_SAVE_IMAGES].value == exp["xray_save_images"], rel_path
        assert scan.states[RequirementKind.XRAY].value == exp["xray"], rel_path
        assert scan.states[RequirementKind.PRESS_FIT].value == exp["press_fit"], rel_path
        assert scan.states[RequirementKind.AQUANOX].value == exp["aquanox"], rel_path


@pytest.mark.skipif(
    not CORPUS_DIR.is_dir() or not EXPECTATIONS_PATH.is_file(),
    reason="Local BOM corpus backend/data/data not present",
)
def test_corpus_boms_match_phase54_expectations(tmp_path: Path) -> None:
    exp = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))["corpus_bom_expectations"]
    bom_files = sorted(
        p for p in CORPUS_DIR.rglob("*.xlsx")
        if p.is_file() and "audit bom" in p.name.lower() and not p.name.startswith("~$")
    )
    assert len(bom_files) == exp["total_boms"]

    empty_eco = tmp_path / "empty_eco.docx"
    doc = docx.Document()
    doc.add_paragraph("Normal build notes")
    doc.save(str(empty_eco))
    eco_res = read_eco_units(empty_eco, DEFAULT_VOCABULARY)

    xr_count = 0
    pf_note_count = 0
    pf_desc_count = 0
    aq_count = 0
    unreadable_count = 0
    save_img_files: list[str] = []
    pf_pats = DEFAULT_VOCABULARY.patterns[RequirementKind.PRESS_FIT]

    for bf in bom_files:
        bom_res = read_bom_units(bf)
        if bom_res.is_err:
            unreadable_count += 1
            continue
        units = bom_res.value
        scan = decide_requirements(
            eco=eco_res,
            bom=bom_res,
            traveler=None,
            inputs=ScanInputs(
                notes_path=empty_eco,
                notes_hash="h_eco",
                bom_path=bf,
                bom_hash="h_bom",
                traveler_wash_text=None,
                vocabulary=DEFAULT_VOCABULARY,
            ),
        )
        if scan.states[RequirementKind.XRAY] == RequirementState.DETECTED:
            xr_count += 1
        if scan.states[RequirementKind.AQUANOX] == RequirementState.DETECTED:
            aq_count += 1
        if scan.states[RequirementKind.XRAY_SAVE_IMAGES] == RequirementState.DETECTED:
            save_img_files.append(bf.name)

        has_pf_note = False
        has_pf_desc = False
        for u in units:
            if u.source not in (UnitSource.BOM_NOTE, UnitSource.BOM_DESCRIPTION):
                continue
            toks = normalise(u.text)
            if any(
                not is_negated(toks, s, e, DEFAULT_VOCABULARY)
                for pat in pf_pats
                for s, e in find_matches(toks, pat, DEFAULT_VOCABULARY)
            ):
                if u.source == UnitSource.BOM_NOTE:
                    has_pf_note = True
                else:
                    has_pf_desc = True
        if has_pf_note:
            pf_note_count += 1
        if has_pf_desc:
            pf_desc_count += 1

    assert unreadable_count == exp["unreadable_boms"]
    assert xr_count == exp["xray_detected"]
    assert pf_note_count == exp["press_fit_by_note"]
    assert pf_desc_count == exp["press_fit_by_description"]
    assert aq_count == exp["aquanox_detected"]
    assert len(save_img_files) == exp["xray_save_images_detected"]
    assert save_img_files == exp["xray_save_images_files"]


@pytest.mark.skipif(
    not EXPECTATIONS_PATH.is_file(),
    reason="Local expectations file backend/data/phase54_expectations.json not present",
)
def test_acceptance_on_sample_jobs_and_b143272_lifecycle(tmp_path: Path, monkeypatch) -> None:
    samples = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))["acceptance_samples"]
    for job, meta in samples.items():
        job_dir = DATA_DIR / meta["folder"]
        if not job_dir.is_dir():
            pytest.skip(f"Sample job directory {meta['folder']} not present")

    root = tmp_path / "cockpit_data"
    root.mkdir()
    monkeypatch.setenv("COCKPIT_APP_DATA", str(root))
    config = resolve_config(root / "v1")
    bootstrapped = bootstrap(config)

    try:
        for job, meta in samples.items():
            job_dir = DATA_DIR / meta["folder"]
            paths = sorted(
                p for p in job_dir.iterdir()
                if p.is_file() and not p.name.startswith("~$")
            )
            bootstrapped.ingestion_service.ingest(paths, requirements_vocabulary=DEFAULT_VOCABULARY)

        digests = {d.part_number: d for d in bootstrapped.audit_read_svc.list_open()}

        # Verify B143355: X-ray PENDING; XR detected; AQ and PF blank; Wash NC
        d_355 = digests["B143355"]
        assert xray_cell_state(d_355.requirements, d_355.xray_tick) == XrayCellState.PENDING
        assert d_355.requirements.states[RequirementKind.XRAY] == RequirementState.DETECTED
        assert d_355.requirements.states[RequirementKind.AQUANOX] == RequirementState.ABSENT
        assert d_355.requirements.states[RequirementKind.PRESS_FIT] == RequirementState.ABSENT
        assert wash_state({"process_clean": d_355.process_clean}) == WashState.NO_CLEAN

        # Verify B143272: X-ray PENDING; XR from table and note; AQ blank despite aqueous note; Wash NC
        d_272 = digests["B143272"]
        assert xray_cell_state(d_272.requirements, d_272.xray_tick) == XrayCellState.PENDING
        assert d_272.requirements.states[RequirementKind.XRAY] == RequirementState.DETECTED
        assert d_272.requirements.states[RequirementKind.AQUANOX] == RequirementState.ABSENT
        assert d_272.requirements.states[RequirementKind.PRESS_FIT] == RequirementState.ABSENT
        assert wash_state({"process_clean": d_272.process_clean}) == WashState.NO_CLEAN
        ev_272 = bootstrapped.audit_repo.requirements_repo.find_evidence(d_272.audit_id)
        xr_ev_272 = [e for e in ev_272 if e.kind == RequirementKind.XRAY and not e.is_negated]
        assert any(e.location == "ECO X-ray table" for e in xr_ev_272)
        assert any(e.location != "ECO X-ray table" and e.source == UnitSource.ECO_TEXT for e in xr_ev_272)

        # Verify B141837: X-ray PENDING; XR from table and note; AQ blank despite "after-wash"; Wash C
        d_837 = digests["B141837"]
        assert xray_cell_state(d_837.requirements, d_837.xray_tick) == XrayCellState.PENDING
        assert d_837.requirements.states[RequirementKind.XRAY] == RequirementState.DETECTED
        assert d_837.requirements.states[RequirementKind.AQUANOX] == RequirementState.ABSENT
        assert wash_state({"process_clean": d_837.process_clean}) == WashState.CLEAN

        # Verify B143206: X-ray blank (marker then N/A, no notes); AQ from ECO and traveler; Wash NC
        d_206 = digests["B143206"]
        assert xray_cell_state(d_206.requirements, d_206.xray_tick) == XrayCellState.BLANK
        assert d_206.requirements.states[RequirementKind.XRAY_SAVE_IMAGES] == RequirementState.ABSENT
        assert d_206.requirements.states[RequirementKind.XRAY] == RequirementState.ABSENT
        assert d_206.requirements.states[RequirementKind.AQUANOX] == RequirementState.DETECTED
        assert wash_state({"process_clean": d_206.process_clean}) == WashState.NO_CLEAN
        ev_206 = bootstrapped.audit_repo.requirements_repo.find_evidence(d_206.audit_id)
        aq_sources_206 = {e.source for e in ev_206 if e.kind == RequirementKind.AQUANOX and not e.is_negated}
        assert UnitSource.ECO_TEXT in aq_sources_206
        assert UnitSource.TRAVELER_WASH in aq_sources_206

        # B143272 lifecycle (§15.4):
        # 1. Completing while PENDING shows the warning
        warn = pending_xray_warning(bootstrapped.conn, d_272.audit_id, DEFAULT_VOCABULARY)
        assert warn == "Its X-ray & save-images task isn't ticked."

        # 2. Tick B143272 -> becomes DONE
        assert set_xray_images_done(bootstrapped.conn, d_272.audit_id, True).is_ok
        d_272_ticked = {d.part_number: d for d in bootstrapped.audit_read_svc.list_open()}["B143272"]
        assert xray_cell_state(d_272_ticked.requirements, d_272_ticked.xray_tick) == XrayCellState.DONE
        assert pending_xray_warning(bootstrapped.conn, d_272.audit_id, DEFAULT_VOCABULARY) is None

        # 3. Re-scanning an unchanged copy keeps it DONE
        bootstrapped.conn.execute(
            "UPDATE source_files SET file_hash = 'rehashed_same_content' WHERE audit_id = ? AND file_category = 'Notes'",
            (d_272.audit_id,),
        )
        bootstrapped.conn.commit()
        run_pass(bootstrapped.conn, stale_scan_jobs(bootstrapped.conn, DEFAULT_VOCABULARY), DEFAULT_VOCABULARY)
        d_272_same = {d.part_number: d for d in bootstrapped.audit_read_svc.list_open()}["B143272"]
        assert xray_cell_state(d_272_same.requirements, d_272_same.xray_tick) == XrayCellState.DONE

        # 4. An edited copy whose X-ray table gains a row turns it DONE_STALE
        notes_sf = [
            sf for sf in bootstrapped.source_file_repo.list_for_audit(d_272.audit_id)
            if sf.file_category == "Notes"
        ][0]
        doc_272 = docx.Document(str(notes_sf.local_storage_path))
        for tbl in doc_272.tables:
            if tbl.rows and tbl.rows[0].cells[0].text.strip().lower().startswith("find"):
                new_cells = tbl.add_row().cells
                new_cells[0].text = "99"
                new_cells[1].text = "NEW-BGA"
                new_cells[2].text = "1"
                new_cells[3].text = "U999"
                break
        doc_272.save(str(notes_sf.local_storage_path))
        new_hash = sha256_hex(notes_sf.local_storage_path)
        bootstrapped.conn.execute(
            "UPDATE source_files SET file_hash = ? WHERE id = ?",
            (new_hash, notes_sf.id),
        )
        bootstrapped.conn.commit()

        run_pass(bootstrapped.conn, stale_scan_jobs(bootstrapped.conn, DEFAULT_VOCABULARY), DEFAULT_VOCABULARY)
        d_272_edited = {d.part_number: d for d in bootstrapped.audit_read_svc.list_open()}["B143272"]
        assert xray_cell_state(d_272_edited.requirements, d_272_edited.xray_tick) == XrayCellState.DONE_STALE
        assert (
            pending_xray_warning(bootstrapped.conn, d_272.audit_id, DEFAULT_VOCABULARY)
            == "Its X-ray & save-images tick predates a change in the X-ray requirements."
        )
    finally:
        bootstrapped.conn.close()
