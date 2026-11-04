import pathlib
from types import SimpleNamespace

import openpyxl
import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QTextDocument
from PyQt6.QtWidgets import QApplication, QWidget

from cockpit.ingestion.parsers.audit_bom import (
     BomNote,
    CLEANING_RULES_VERSION,
    CleaningReport,
    CleaningSummary,
    ColumnName,
    DesignatorRejected,
    EMPTY_CLEANING_SUMMARY,
    EnclosingQuotesRemoved,
    FreeTextInRefDes,
    LineRemovedByStrike,
    MountType,
    MountTypeReplacedByStrike,
    NoteExtracted,
)
from cockpit.services.notes_renderer import RenderPalette, _CellPainter, _MediaBudget
from cockpit.ui.bootstrap import bootstrap
from cockpit.ui.config import resolve_config
from cockpit.ui.main_window import MainWindow
from cockpit.ui.theme import ThemeLoader
from cockpit.ui.widgets.cleaning_dialogs import (
    CleaningChangesDialog,
    CleaningReportDialog,
)
from cockpit.ui.widgets.toast import Toast
from cockpit.ui.workers.ingestion_worker import AuditSummary, IngestionWorker


@pytest.fixture
def app_config(tmp_path, monkeypatch):
    root = tmp_path / "cockpit_data"
    root.mkdir()
    monkeypatch.setenv("COCKPIT_APP_DATA", str(root))
    return resolve_config(root / "v1")


@pytest.fixture
def bootstrapped_app(app_config):
    return bootstrap(app_config)


@pytest.fixture
def seeded_audit(bootstrapped_app, tmp_path):
    bom_path = tmp_path / "ASSY-A AUDIT BOM.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(["Find#", "PartNum", "Ref_Des", "Description", "SMT/THT"])
    ws.append([1, "RC0805FR-0710KL", "R1 (<b>bold note</b>)", "RES 10K", "SMT"])
    wb.save(str(bom_path))
    wb.close()

    conn = bootstrapped_app.conn
    conn.execute(
        "INSERT INTO active_audits "
        "(id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (1, 'ASSY-A', 'WO-1', '', 10, 'Not Clear', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source_files "
        "(id, audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (10, 1, 'BOM', 'bom.xlsx', ?, 'h1', '2026-01-01T00:00:00')",
        (str(bom_path),),
    )
    conn.execute(
        "INSERT INTO audit_bom_components "
        "(source_file_id, component_mpn, ref_des, mount_type, description, find_number) "
        "VALUES (10, 'RC0805FR-0710KL', 'R1', 'S', 'RES 10K', '1')"
    )
    return 1


@pytest.fixture
def make_window(qtbot, bootstrapped_app, tmp_path):
    windows = []

    def build():
        ui_dir = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui"
        theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
        settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
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
        windows.append(window)
        return window

    yield build

    for window in windows:
        for _, dlg in list(window._cleaning_report_dialogs.values()):
            dlg.close()
        window.shutdown_render_thread(timeout_ms=2000)


# ---- 1. Progress line & toast text, positional AuditSummary ----------------

def test_positional_audit_summary_and_progress_and_toast(qtbot):
    # Positional construction still works and defaults cleaning
    s_pos = AuditSummary(1, "ASSY-A", "WO-1", 3, 2)
    assert s_pos.cleaning == EMPTY_CLEANING_SUMMARY

    # Worker progress line annotation for BOM_PARSED
    worker = IngestionWorker(SimpleNamespace(), [])
    emitted = []
    worker.progress_signal.connect(lambda stg, ann: emitted.append((stg, ann)))
    cleaning_summary = CleaningSummary(
        routine_counts={
            NoteExtracted: 3,
            EnclosingQuotesRemoved: 12,
        },
        consequential=[LineRemovedByStrike(sheet_row=14, struck_part_number="MPN-X")],
    )
    from cockpit.ingestion.progress import ProgressEvent, ProgressStage
    worker._on_progress(
        ProgressEvent(
            stage=ProgressStage.BOM_PARSED,
            detail={"header_layout": "canonical", "cleaning": cleaning_summary},
        )
    )
    assert emitted == [("bom_parsed", "canonical layout \u00b7 cleaned: 3 notes, 12 quoted values")]

    # Toast text with 1 change to review and 2 changes to review
    parent = QWidget()
    qtbot.addWidget(parent)
    toast = Toast(parent)

    summary_1 = AuditSummary(1, "ASSY-A", "WO-1", 7, 4, cleaning=cleaning_summary)
    toast.show_success(summary_1)
    assert toast.subtitle_label.text() == (
        "7 THT items, 4 ECO items \u00b7 cleaned: 3 notes, 12 quoted values \u00b7 1 change to review"
    )

    cleaning_summary_2 = CleaningSummary(
        routine_counts={},
        consequential=[
            LineRemovedByStrike(sheet_row=14, struck_part_number="MPN-X"),
            MountTypeReplacedByStrike(sheet_row=21, struck="SMT", kept=MountType.THT),
        ],
    )
    summary_2 = AuditSummary(1, "ASSY-A", "WO-1", 7, 4, cleaning=cleaning_summary_2)
    toast.show_success(summary_2)
    assert toast.subtitle_label.text() == (
        "7 THT items, 4 ECO items \u00b7 2 changes to review"
    )


# ---- 2. CleaningChangesDialog: modal, consequential only, before split offer

def test_changes_dialog_modal_consequential_only_and_before_split(
    make_window, seeded_audit, monkeypatch
):
    window = make_window()
    call_order = []

    monkeypatch.setattr(
        window,
        "_offer_split_after_ingest",
        lambda aid: call_order.append(("split_offer", aid)),
    )
    monkeypatch.setattr(
        window,
        "_on_audit_cleaning_report_requested",
        lambda aid: call_order.append(("full_report", aid)),
    )

    # Routine-only cleaning -> CleaningChangesDialog does NOT open
    dialog_opened = []

    class SpyDialog(CleaningChangesDialog):
        def exec(self):
            dialog_opened.append(self.isModal())
            call_order.append(("changes_dialog", None))
            return super().accept()

    import cockpit.ui.widgets.cleaning_dialogs as cd_mod
    monkeypatch.setattr(cd_mod, "CleaningChangesDialog", SpyDialog)

    routine_only = CleaningSummary(routine_counts={NoteExtracted: 2}, consequential=[])
    window._on_ingest_succeeded(AuditSummary(seeded_audit, "ASSY-A", "WO-1", 0, 0, routine_only))
    assert dialog_opened == []
    assert call_order == [("split_offer", seeded_audit)]

    # Consequential cleaning -> CleaningChangesDialog opens (modal) BEFORE split offer
    call_order.clear()
    conseq = CleaningSummary(
        routine_counts={},
        consequential=[LineRemovedByStrike(sheet_row=14, struck_part_number="MPN-1")],
    )
    window._on_ingest_succeeded(AuditSummary(seeded_audit, "ASSY-A", "WO-1", 0, 0, conseq))
    assert dialog_opened == [True]
    assert call_order == [("changes_dialog", None), ("split_offer", seeded_audit)]

    # Clicking "Full report..." opens CleaningReportDialog after split offer
    call_order.clear()

    class FullReportSpyDialog(CleaningChangesDialog):
        def exec(self):
            call_order.append(("changes_dialog_full", None))
            self._on_full_report_clicked()
            return 1

    monkeypatch.setattr(cd_mod, "CleaningChangesDialog", FullReportSpyDialog)
    window._on_ingest_succeeded(AuditSummary(seeded_audit, "ASSY-A", "WO-1", 0, 0, conseq))
    assert call_order == [
        ("changes_dialog_full", None),
        ("split_offer", seeded_audit),
        ("full_report", seeded_audit),
    ]


# ---- 3. More menu entry: second request raises open dialog -----------------

def test_more_menu_entry_and_second_request_raises_open_dialog(
    make_window, seeded_audit, monkeypatch
):
    window = make_window()
    window._audit_view.load(seeded_audit)

    actions_bar = window._audit_view._actions_bar
    actions_bar._rebuild_actions_menu()
    menu_actions = {a.text(): a for a in actions_bar.actions_menu.actions() if not a.isSeparator()}
    menu_texts = list(menu_actions.keys())
    assert "2nd OPS\u2026" in menu_texts
    assert "BOM cleaning report\u2026" in menu_texts
    assert menu_texts.index("BOM cleaning report\u2026") == menu_texts.index("2nd OPS\u2026") + 1
    assert menu_actions["BOM cleaning report\u2026"].isEnabled()

    window._on_audit_cleaning_report_requested(seeded_audit)
    assert seeded_audit in window._cleaning_report_dialogs
    serial1, dlg1 = window._cleaning_report_dialogs[seeded_audit]

    raised = []
    monkeypatch.setattr(dlg1, "raise_", lambda: raised.append("raise"))
    monkeypatch.setattr(dlg1, "activateWindow", lambda: raised.append("activate"))

    # Second request raises existing dialog without creating a new one
    window._on_audit_cleaning_report_requested(seeded_audit)
    serial2, dlg2 = window._cleaning_report_dialogs[seeded_audit]
    assert serial1 == serial2
    assert dlg1 is dlg2
    assert raised == ["raise", "activate"]

    dlg1.close()
    assert seeded_audit not in window._cleaning_report_dialogs


# ---- 4. Closing report dialog mid-read drops late result safely ------------

def test_closing_report_dialog_mid_read_leaves_no_live_connection(
    qtbot, bootstrapped_app, seeded_audit
):
    dlg = CleaningReportDialog(
        seeded_audit,
        bootstrapped_app.source_file_repo,
        auto_start=False,
    )
    qtbot.addWidget(dlg)

    # Simulate an active worker without starting a real OS thread
    from cockpit.ui.widgets.cleaning_dialogs import CleaningReportWorker
    worker = CleaningReportWorker(pathlib.Path("dummy.xlsx"))
    worker.report_ready.connect(dlg._on_report_ready)
    worker.read_failed.connect(dlg._on_read_failed)
    dlg._worker = worker

    # Close mid-read
    dlg.close()
    assert dlg._worker is None

    # Emit late result after close -> dropped without updating dialog or raising
    late_report = CleaningReport(
        rules_version=CLEANING_RULES_VERSION,
        sheet_name="AUDIT BOM",
        sheet_is_visible=True,
        header_row=1,
        actions=[LineRemovedByStrike(sheet_row=2, struck_part_number="MPN")],
        rejected_rows=[],
    )
    worker.report_ready.emit(late_report)
    assert dlg.table.rowCount() == 0


# ---- 5. Plain-text rendering of a cell containing <b> ----------------------

def test_plain_text_rendering_of_html_like_cell(qtbot, bootstrapped_app, seeded_audit):
    summary = CleaningSummary(
        routine_counts={},
        consequential=[
            FreeTextInRefDes(sheet_row=3, text="<b>DO NOT RENDER HTML</b>"),
        ],
    )
    changes_dlg = CleaningChangesDialog(summary)
    qtbot.addWidget(changes_dlg)
    assert changes_dlg.intro_label.textFormat() == Qt.TextFormat.PlainText
    assert changes_dlg.table.item(0, 1).text() == (
        "Free text in Ref_Des (<b>DO NOT RENDER HTML</b>) kept as a note; no designators."
    )

    report_dlg = CleaningReportDialog(
        seeded_audit,
        bootstrapped_app.source_file_repo,
        auto_start=False,
    )
    qtbot.addWidget(report_dlg)
    report = CleaningReport(
        rules_version=CLEANING_RULES_VERSION,
        sheet_name="<b>Sheet</b>",
        sheet_is_visible=False,
        header_row=1,
        actions=[
            NoteExtracted(note=BomNote(2, "1", ColumnName.REF_DES, "<b>bold note</b>")),
        ],
        rejected_rows=[
            DesignatorRejected(sheet_row=4, cell_text="<b>bad</b>", token="<b>bad</b>"),
        ],
    )
    report_dlg._on_report_ready(report)
    assert report_dlg.header_label.textFormat() == Qt.TextFormat.PlainText
    assert "<b>Sheet</b> (hidden)" in report_dlg.header_label.text()
    assert "<b>bold note</b>" in report_dlg.table.item(0, 1).text()
    assert report_dlg.rejected_table.item(0, 1).text() == "<b>bad</b>"


# ---- 6. Strike-out in the build-notes pane (_CellPainter._char_format) -----

def test_notes_renderer_char_format_strike_and_double_strike():
    doc = QTextDocument()
    palette = RenderPalette(
        page_background_rgb="#ffffff",
        default_text_rgb="#000000",
        placeholder_border_rgb="#999999",
        placeholder_text_rgb="#666666",
    )
    painter = _CellPainter(doc, None, palette, _MediaBudget(1000), [], set())

    def _fake_run(strike=False, double_strike=False):
        return SimpleNamespace(
            bold=False,
            italic=False,
            underline=False,
            font=SimpleNamespace(
                strike=strike,
                double_strike=double_strike,
                color=None,
                size=None,
                name=None,
            ),
        )

    fmt_none = painter._char_format(_fake_run(strike=False, double_strike=False))
    assert fmt_none.fontStrikeOut() is False

    fmt_strike = painter._char_format(_fake_run(strike=True, double_strike=False))
    assert fmt_strike.fontStrikeOut() is True

    fmt_dstrike = painter._char_format(_fake_run(strike=False, double_strike=True))
    assert fmt_dstrike.fontStrikeOut() is True
