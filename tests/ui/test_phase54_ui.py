"""Phase 54 widget and UI tests (§15.2)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import docx
import openpyxl
import pytest
from PyQt6.QtCore import QModelIndex, QRect, QSettings, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem

from cockpit.persistence.repositories.requirements import set_xray_images_done
from cockpit.persistence.types import ActiveAudit, AuditStatus
from cockpit.services.requirements import (
     BadgeState,
    DEFAULT_VOCABULARY,
    EvidenceCache,
    RequirementBadges,
    RequirementEvidence,
    RequirementKind,
    RequirementState,
    RequirementSummary,
    UnitSource,
    XrayCellState,
    XrayTick,
    build_requirement_badges,
)
from cockpit.services.repeat import RepeatMarker
from cockpit.services.views import (
    AuditIdentityBanner,
    OpenAuditDigest,
)
from cockpit.ui.bootstrap import bootstrap
from cockpit.ui.config import resolve_config
from cockpit.ui.font_scale_controller import FontScaleController
from cockpit.ui.main_window import MainWindow
from cockpit.ui.theme import ThemeLoader
from cockpit.ui.ui_prefs import RuntimeCalcSettingsController, StyleController
from cockpit.ui.widgets.audit_identity_bar import AuditIdentityBar
from cockpit.ui.widgets.open_audit_picker import (
    XRAY_STALE_ROLE,
    CenteredCheckDelegate,
    Column,
    OpenAuditPicker,
    _DEFAULT_COLUMN_WIDTHS,
)
from cockpit.ui.widgets.replace_confirm_dialog import ReplaceConfirmDialog, _casualty_lines
from cockpit.ui.widgets.settings_dialog import SettingsDialog, TryProcessRequirementsDialog
from cockpit.services.replacement import ReplacementCasualty, summarise_family


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_digest(
    audit_id: int = 1,
    part_number: str = "ASSY-1",
    requirements: RequirementSummary | None = None,
    xray_tick: XrayTick = XrayTick(),
) -> OpenAuditDigest:
    return OpenAuditDigest(
        audit_id=audit_id,
        part_number=part_number,
        work_order_ref=f"WO-{audit_id}",
        split_suffix="",
        quantity=10,
        status=AuditStatus.NOT_CLEAR,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        date_ingested=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ship_date=date(2026, 1, 10),
        lead_time_days=5,
        repeat=RepeatMarker(label="", reference="", is_new_assembly=True, display="NEW"),
        classification="NON-ITAR",
        assembly_class=2,
        process="Lead-Free",
        feeder_setuptime=1.0,
        smt_runtime=1.0,
        tht_runtime=1.0,
        aoi_runtime=1.0,
        ops_runtime=1.0,
        shipping_runtime=1.0,
        start_by=date(2026, 1, 5),
        ops_per_board_min=None,
        is_itar=False,
        process_clean="NC",
        article_revision="FA",
        is_labeled=False,
        are_photos_uploaded=False,
        requirements=requirements,
        xray_tick=xray_tick,
    )


def _summary(
    *,
    aq: RequirementState = RequirementState.ABSENT,
    pf: RequirementState = RequirementState.ABSENT,
    xr: RequirementState = RequirementState.ABSENT,
    save: RequirementState = RequirementState.ABSENT,
    reasons: dict[RequirementKind, str] | None = None,
    task: str | None = None,
    fp: str = "fp-1",
) -> RequirementSummary:
    return RequirementSummary(
        states={
            RequirementKind.AQUANOX: aq,
            RequirementKind.PRESS_FIT: pf,
            RequirementKind.XRAY: xr,
            RequirementKind.XRAY_SAVE_IMAGES: save,
        },
        unknown_reasons=reasons or {},
        xray_task=task,
        fingerprint=fp,
        scanned_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. Audit List: columns, checkability, delegate, tooltips, cache, sort, search
# ---------------------------------------------------------------------------

def test_audit_list_columns_checkability_tooltips_cache_sort_and_search(qtbot) -> None:
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)

    # Column indices and widths
    assert Column.AQUANOX == 11
    assert Column.PRESS_FIT == 12
    assert Column.XRAY == 22
    assert Column.ARTICLE_REVISION == 23
    assert _DEFAULT_COLUMN_WIDTHS[Column.AQUANOX] == 40
    assert _DEFAULT_COLUMN_WIDTHS[Column.PRESS_FIT] == 40
    assert _DEFAULT_COLUMN_WIDTHS[Column.XRAY] == 55

    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    d_not_scanned = _make_digest(1, "P-UNSCANNED", requirements=None)
    d_pending = _make_digest(
        2,
        "P-PENDING",
        requirements=_summary(
            aq=RequirementState.DETECTED,
            pf=RequirementState.DETECTED,
            xr=RequirementState.DETECTED,
            save=RequirementState.DETECTED,
            task="t1",
            fp="fp-2",
        ),
        xray_tick=XrayTick(False, None, None),
    )
    d_done = _make_digest(
        3,
        "P-DONE",
        requirements=_summary(xr=RequirementState.DETECTED, save=RequirementState.DETECTED, task="t1", fp="fp-3"),
        xray_tick=XrayTick(True, "t1", now),
    )
    d_stale = _make_digest(
        4,
        "P-STALE",
        requirements=_summary(xr=RequirementState.DETECTED, save=RequirementState.DETECTED, task="t2", fp="fp-4"),
        xray_tick=XrayTick(True, "t1", now),
    )
    d_xronly = _make_digest(
        5,
        "P-XRONLY",
        requirements=_summary(xr=RequirementState.DETECTED, save=RequirementState.ABSENT, fp="fp-5"),
    )
    d_unknown = _make_digest(
        6,
        "P-UNKNOWN",
        requirements=_summary(
            aq=RequirementState.UNKNOWN,
            pf=RequirementState.UNKNOWN,
            xr=RequirementState.UNKNOWN,
            save=RequirementState.UNKNOWN,
            reasons={RequirementKind.XRAY_SAVE_IMAGES: "ECO file missing"},
            fp="fp-6",
        ),
    )
    d_blank = _make_digest(7, "P-BLANK", requirements=_summary(fp="fp-7"))

    load_calls: list[int] = []

    def fake_loader(aid: int) -> list[RequirementEvidence]:
        load_calls.append(aid)
        return [
            RequirementEvidence(
                kind=RequirementKind.AQUANOX,
                source=UnitSource.ECO_TEXT,
                location="ECO paragraph 1",
                excerpt="Wash in <script>alert(1)</script> Aquanox",
                is_negated=False,
            )
        ]

    picker.model.set_evidence_loader(fake_loader)
    picker.populate([d_not_scanned, d_pending, d_done, d_stale, d_xronly, d_unknown, d_blank])

    # Find data rows by audit_id
    row_by_id: dict[int, int] = {}
    for r in range(picker.model.rowCount()):
        ud = picker.model.index(r, 0).data(Qt.ItemDataRole.UserRole)
        if ud and "digest" in ud:
            row_by_id[ud["digest"].audit_id] = r

    # Checkability per state
    for aid, expected_checkable in [
        (1, False),
        (2, True),
        (3, True),
        (4, True),
        (5, False),
        (6, False),
        (7, False),
    ]:
        idx = picker.model.index(row_by_id[aid], Column.XRAY)
        is_chk = bool(idx.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        assert is_chk is expected_checkable

    # Amber role on DONE_STALE only
    assert picker.model.index(row_by_id[4], Column.XRAY).data(XRAY_STALE_ROLE) is True
    assert picker.model.index(row_by_id[3], Column.XRAY).data(XRAY_STALE_ROLE) is False

    # CenteredCheckDelegate paints all X-ray states without crashing
    delegate = CenteredCheckDelegate(picker.table_view)
    img = QImage(100, 30, QImage.Format.Format_ARGB32)
    painter = QPainter(img)
    try:
        for aid in row_by_id:
            opt = QStyleOptionViewItem()
            opt.rect = QRect(0, 0, 100, 30)
            delegate.paint(painter, opt, picker.model.index(row_by_id[aid], Column.XRAY))
    finally:
        painter.end()

    # NOT_SCANNED tooltip
    ns_tip = picker.model.index(row_by_id[1], Column.XRAY).data(Qt.ItemDataRole.ToolTipRole)
    assert "Not scanned yet; Cockpit reads it shortly" in ns_tip

    # Escaped tooltip + EvidenceCache hit on second request, miss after fingerprint changes
    aq_idx = picker.model.index(row_by_id[2], Column.AQUANOX)
    tip1 = aq_idx.data(Qt.ItemDataRole.ToolTipRole)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in tip1
    assert "<script>" not in tip1
    assert load_calls == [2]

    # Second hover hits cache -> no new call to fake_loader
    tip2 = aq_idx.data(Qt.ItemDataRole.ToolTipRole)
    assert tip2 == tip1
    assert load_calls == [2]

    # Rescan with new fingerprint -> cache misses and loads again
    d_pending_rescanned = _make_digest(
        2,
        "P-PENDING",
        requirements=_summary(
            aq=RequirementState.DETECTED,
            pf=RequirementState.DETECTED,
            xr=RequirementState.DETECTED,
            save=RequirementState.DETECTED,
            task="t1",
            fp="fp-2-new",
        ),
    )
    picker.populate([d_pending_rescanned])
    r2 = next(
        r
        for r in range(picker.model.rowCount())
        if (picker.model.index(r, 0).data(Qt.ItemDataRole.UserRole) or {}).get("digest")
    )
    _ = picker.model.index(r2, Column.AQUANOX).data(Qt.ItemDataRole.ToolTipRole)
    assert load_calls == [2, 2]

    # Sort order for XRAY: PENDING -> DONE_STALE -> DONE -> XR_ONLY -> UNKNOWN/NOT_SCANNED -> BLANK
    picker.populate([d_blank, d_xronly, d_done, d_stale, d_pending])
    picker.model.set_sort(Column.XRAY, True)
    ordered_ids = [
        picker.model.index(r, 0).data(Qt.ItemDataRole.UserRole)["digest"].audit_id
        for r in range(picker.model.rowCount())
        if (picker.model.index(r, 0).data(Qt.ItemDataRole.UserRole) or {}).get("digest")
    ]
    assert ordered_ids == [2, 4, 3, 5, 7]

    # Search tokens
    picker.populate([d_pending, d_done, d_stale, d_xronly, d_blank])
    for token, expected_ids in [
        ("aquanox", [2]),
        ("pressfit", [2]),
        ("xray", [2, 3, 4, 5]),
        ("xraypending", [2]),
        ("xraydone", [3]),
        ("xraystale", [4]),
    ]:
        picker.search_edit.setText(token)
        shown = [
            picker.model.index(r, 0).data(Qt.ItemDataRole.UserRole)["digest"].audit_id
            for r in range(picker.model.rowCount())
            if (picker.model.index(r, 0).data(Qt.ItemDataRole.UserRole) or {}).get("digest")
        ]
        assert sorted(shown) == expected_ids
    picker.search_edit.clear()


# ---------------------------------------------------------------------------
# 2. Identity bar: mirrors list, BadgeState.NOT_SCANNED renders '?', tick syncs
# ---------------------------------------------------------------------------

def test_identity_bar_states_and_tick_signal(qtbot) -> None:
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    bar.show()

    def _banner(badges: RequirementBadges) -> AuditIdentityBanner:
        return AuditIdentityBanner(
            sales_order="SO-1",
            part_number="ASSY-1",
            quantity="10",
            lead_time_days="5",
            assembly_class="Class 2",
            process="Lead-Free NC",
            customer="ACME",
            repeat_marker=RepeatMarker(label="", reference="", is_new_assembly=True, display="NEW"),
            status="Not Clear",
            is_itar=False,
            requirements=badges,
        )

    # NOT_SCANNED renders '?' on AQ, PF, and X-ray
    bar.set_identity(
        _banner(
            RequirementBadges(
                aquanox=BadgeState.NOT_SCANNED,
                press_fit=BadgeState.NOT_SCANNED,
                xray_cell=XrayCellState.NOT_SCANNED,
            )
        )
    )
    assert bar.aq_lbl.isVisible() and bar.aq_lbl.text() == "AQ ?"
    assert bar.pf_lbl.isVisible() and bar.pf_lbl.text() == "PF ?"
    assert bar.xray_lbl.isVisible() and bar.xray_lbl.text() == "X-ray ?"
    assert not bar.xray_chk.isVisible()

    # DETECTED AQ & PF, PENDING X-ray -> shows AQ, PF chips and unchecked checkbox
    s_pend = _summary(
        aq=RequirementState.DETECTED,
        pf=RequirementState.DETECTED,
        xr=RequirementState.DETECTED,
        save=RequirementState.DETECTED,
        task="t1",
    )
    bar.set_requirements_context(42, s_pend, XrayTick(False, None, None))
    bar.set_identity(_banner(build_requirement_badges(s_pend, XrayTick(False, None, None))))
    assert bar.aq_lbl.isVisible() and bar.aq_lbl.text() == "AQ"
    assert bar.pf_lbl.isVisible() and bar.pf_lbl.text() == "PF"
    assert bar.xray_chk.isVisible() and not bar.xray_chk.isChecked()
    assert not bar.xray_lbl.isVisible()

    # Ticking emits xray_toggle_requested(42, True)
    toggled: list[tuple[int, bool]] = []
    bar.xray_toggle_requested.connect(lambda aid, chk: toggled.append((aid, chk)))
    bar.xray_chk.setChecked(True)
    assert toggled == [(42, True)]


# ---------------------------------------------------------------------------
# 3. Settings › Process Requirements: validation, hint, Try on an audit, Export/Import
# ---------------------------------------------------------------------------

@pytest.fixture
def app_config(tmp_path, monkeypatch):
    root = tmp_path / "cockpit_data"
    root.mkdir()
    monkeypatch.setenv("COCKPIT_APP_DATA", str(root))
    return resolve_config(root / "v1")


@pytest.fixture
def bootstrapped_app(app_config):
    app = bootstrap(app_config)
    yield app
    app.conn.close()


def test_settings_process_requirements_validation_try_and_import_export(
    qtbot, bootstrapped_app, tmp_path: Path
) -> None:
    ui_dir = Path(__file__).parent.parent.parent / "cockpit" / "ui"
    theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    style_ctrl = StyleController(QApplication.instance(), theme, settings)
    font_ctrl = FontScaleController(QApplication.instance(), theme, settings, composer=style_ctrl.compose)
    calc_ctrl = RuntimeCalcSettingsController(settings)

    dlg = SettingsDialog(
        style_ctrl,
        font_ctrl,
        calc_ctrl,
        None,
        bootstrapped_app.config,
        settings=settings,
        conn=bootstrapped_app.conn,
    )
    qtbot.addWidget(dlg)
    dlg.show()

    # Window hint text matches §10.1
    assert dlg.req_window_hint_lbl is not None
    assert "`~ N`: every part within N words of the others. `~ 1` means next to each other." in dlg.req_window_hint_lbl.text()

    # Valid by default -> Save enabled
    assert dlg.req_save_btn.isEnabled()

    # Type an invalid line (two groups without ~ window) -> Save disabled & inline error shown
    xr_edit = dlg.req_pattern_edits[RequirementKind.XRAY]
    xr_edit.setPlainText("xray + location")
    assert not dlg.req_save_btn.isEnabled()
    assert dlg.req_validation_lbl.isVisible()
    assert "line 1" in dlg.req_validation_lbl.text()

    # Restore box defaults -> valid again
    dlg.req_restore_buttons[RequirementKind.XRAY].click()
    assert dlg.req_save_btn.isEnabled()

    # Export then Import round-trip
    aq_edit = dlg.req_pattern_edits[RequirementKind.AQUANOX]
    aq_edit.setPlainText("aquanox\nkyzen")
    assert dlg.req_save_btn.isEnabled()
    vocab_custom = dlg.current_editor_vocabulary().value

    exp_file = tmp_path / "exported_vocab.json"
    from cockpit.services.requirements import export_vocabulary_file
    export_vocabulary_file(exp_file, vocab_custom)

    dlg._restore_all_process_requirements_defaults()
    assert "kyzen" not in aq_edit.toPlainText()

    imp_res = dlg.import_vocabulary_from_path(exp_file)
    assert imp_res.is_ok
    assert "kyzen" in aq_edit.toPlainText()
    assert dlg.req_import_summary_lbl.isVisible()
    assert "added" in dlg.req_import_summary_lbl.text()

    # Try on an audit writes nothing to the database
    eco_p = tmp_path / "eco.docx"
    doc = docx.Document()
    doc.add_paragraph("Wash board with kyzen solution")
    doc.save(str(eco_p))

    bom_p = tmp_path / "bom.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT"])
    ws.append([1, "MPN-1", "R1", "RES 10K", "SMT"])
    wb.save(str(bom_p))
    wb.close()

    conn = bootstrapped_app.conn
    conn.execute(
        "INSERT INTO active_audits "
        "(id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (1, 'ASSY-TRY', 'WO-TRY', '', 5, 'Not Clear', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source_files "
        "(audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (1, 'Notes', 'eco.docx', ?, 'h1', '2026-01-01T00:00:00'), "
        "       (1, 'BOM', 'bom.xlsx', ?, 'h2', '2026-01-01T00:00:00')",
        (str(eco_p), str(bom_p)),
    )
    conn.commit()

    try_dlg = TryProcessRequirementsDialog(conn, vocab_custom, default_audit_id=1, parent=dlg)
    qtbot.addWidget(try_dlg)
    qtbot.waitUntil(lambda: try_dlg._last_scan is not None, timeout=3000)

    assert try_dlg._last_scan.states[RequirementKind.AQUANOX] == RequirementState.DETECTED
    # Nothing was written to audit_requirements!
    assert bootstrapped_app.audit_repo.requirements_repo.find_summary(1) is None


# ---------------------------------------------------------------------------
# 4. Completion warning (both paths, unscanned scanned on the spot, unreadable reason)
#    and Replacement casualty line
# ---------------------------------------------------------------------------

def test_completion_warning_both_paths_and_replacement_casualty(
    qtbot, bootstrapped_app, tmp_path: Path, monkeypatch
) -> None:
    ui_dir = Path(__file__).parent.parent.parent / "cockpit" / "ui"
    theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)

    eco_p = tmp_path / "eco_save.docx"
    doc = docx.Document()
    doc.add_paragraph("Please X-ray and save images for U1")
    doc.save(str(eco_p))

    bom_p = tmp_path / "bom.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT"])
    ws.append([1, "MPN-1", "U1", "BGA", "SMT"])
    wb.save(str(bom_p))
    wb.close()

    conn = bootstrapped_app.conn
    # Audit 1: unscanned initially, but its documents require X-ray & save images
    conn.execute(
        "INSERT INTO active_audits "
        "(id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (1, 'ASSY-1', 'WO-1', '', 5, 'Not Clear', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source_files "
        "(audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (1, 'Notes', 'eco.docx', ?, 'h1', '2026-01-01T00:00:00'), "
        "       (1, 'BOM', 'bom.xlsx', ?, 'h2', '2026-01-01T00:00:00')",
        (str(eco_p), str(bom_p)),
    )
    # Audit 2: missing ECO file -> UNKNOWN with reason
    conn.execute(
        "INSERT INTO active_audits "
        "(id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (2, 'ASSY-2', 'WO-2', '', 5, 'Not Clear', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source_files "
        "(audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (2, 'Notes', 'missing.docx', ?, 'h3', '2026-01-01T00:00:00'), "
        "       (2, 'BOM', 'bom.xlsx', ?, 'h2', '2026-01-01T00:00:00')",
        (str(tmp_path / "missing.docx"), str(bom_p)),
    )
    conn.commit()

    window = MainWindow(
        QApplication.instance(),
        bootstrapped_app,
        bootstrapped_app.audit_read_svc,
        bootstrapped_app.checklist_svc,
        bootstrapped_app.split_svc,
        bootstrapped_app.completion_svc,
        bootstrapped_app.layout_query_svc,
        bootstrapped_app.pdf_renderer,
        bootstrapped_app.holiday_svc,
        theme=theme,
        settings=settings,
    )
    qtbot.addWidget(window)

    # Clear audit 1's scan row to test on-the-spot scanning during completion
    conn.execute("DELETE FROM audit_requirements WHERE audit_id = 1")
    conn.commit()

    seen_bodies: list[str] = []

    def fake_confirm_destructive(title, body="", confirm_label="", parent=None, **kwargs):
        seen_bodies.append(body)
        return False  # Cancel so the audit isn't deleted

    monkeypatch.setattr("cockpit.ui.main_window.confirm_destructive", fake_confirm_destructive)
    monkeypatch.setattr("cockpit.ui.widgets.dialogs.confirm_destructive", fake_confirm_destructive)

    # Path 1: List completion on unscanned Audit 1 -> scans on the spot, warns about PENDING, and stores the scan!
    window._on_complete_requested(1)
    assert len(seen_bodies) == 1
    assert "Its X-ray & save-images task isn't ticked." in seen_bodies[-1]
    assert bootstrapped_app.audit_repo.requirements_repo.find_summary(1) is not None

    # Path 2: Audit View's Complete button on Audit 1 -> also warns!
    window._audit_view.load(1)
    window._audit_view._actions_bar._on_complete_clicked()
    assert len(seen_bodies) == 2
    assert "Its X-ray & save-images task isn't ticked." in seen_bodies[-1]

    # Audit 2 (unreadable ECO) -> warns with the stored reason
    window._on_complete_requested(2)
    assert len(seen_bodies) == 3
    assert "Its X-ray requirements couldn't be read (" in seen_bodies[-1]
    assert "so an X-ray & save-images task may be outstanding." in seen_bodies[-1]

    # Replacement casualty line
    set_xray_images_done(conn, 1, True)
    aud1 = bootstrapped_app.audit_repo.find_by_id(1)
    casualties = summarise_family([aud1], bootstrapped_app.ingestion_service.tht_repo)
    assert casualties[0].xray_images_done is True
    lines = _casualty_lines(casualties[0])
    assert any("X-ray & save images done" in ln for ln in lines)

    window.shutdown_render_thread(timeout_ms=2000)
