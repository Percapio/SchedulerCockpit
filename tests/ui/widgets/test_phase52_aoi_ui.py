"""Phase 52 section 6 — AOI XY: program-folder setting, actions menu, wiring and the dialog."""

import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QGroupBox, QLineEdit

from cockpit.services.aoi.types import BoardSide
from cockpit.settings.program_root import (
    ProgramRootController, ProgramRootProbeResult, ProgramRootState, probe_program_root,
)
from cockpit.settings.source_root import SourceRootController
from cockpit.ui.main_window import MainWindow
from cockpit.ui.widgets import aoi_xy_dialog as dialog_module
from cockpit.ui.widgets.aoi_xy_dialog import AoiDialogPrompts, AoiXyDialog, side_from_file_name
from cockpit.ui.widgets.audit_actions_bar import AuditActionsBar
from cockpit.ui.widgets.qt_lifecycle import _ADMISSIONS, _IN_FLIGHT_PROBES

from tests.services.phase52_builders import JOB, TOP_ROWS, build_job, write_xyrs


@pytest.fixture(autouse=True)
def _clean_registries():
    _IN_FLIGHT_PROBES.clear()
    _ADMISSIONS.clear()
    yield
    _IN_FLIGHT_PROBES.clear()
    _ADMISSIONS.clear()


def ini(tmp_path, name="s.ini") -> QSettings:
    return QSettings(str(tmp_path / name), QSettings.Format.IniFormat)


class ScriptedPrompts(AoiDialogPrompts):
    def __init__(self, directory=None, items=(), files=(), confirm=True, file=""):
        self.directory = directory
        self.items = list(items)
        self.files = list(files)
        self.answer = confirm
        self.file = file
        self.asked = []

    def choose_item(self, parent, title, label, items):
        self.asked.append(("item", list(items)))
        return self.items.pop(0) if self.items else None

    def choose_directory(self, parent, title, start):
        self.asked.append(("directory", start))
        return str(self.directory) if self.directory else ""

    def choose_file(self, parent, title, start, file_filter):
        return str(self.file) if self.file else ""

    def choose_files(self, parent, title, start, file_filter):
        return [str(p) for p in self.files]

    def confirm(self, parent, title, text):
        self.asked.append(("confirm", title))
        return self.answer


def open_dialog(qtbot, job, prompts, settings=None, program_root="job", forbidden=()):
    dialog = AoiXyDialog(
        job_number=job.job,
        stored_bom=job.stored_bom,
        source_root=job.source_root,
        program_root=job.program_root if program_root == "job" else program_root,
        forbidden_roots=forbidden,
        settings=settings,
        prompts=prompts,
    )
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog.is_busy(), timeout=10_000)
    return dialog


def click_and_wait(qtbot, dialog, button):
    button.click()
    qtbot.waitUntil(lambda: not dialog.is_busy(), timeout=10_000)


# --- settings ---------------------------------------------------------------------

def test_program_root_controller_states(tmp_path):
    controller = ProgramRootController(ini(tmp_path))
    assert controller.program_root() == (ProgramRootState.UNSET, None)
    controller.set_program_root("relative\\path")
    assert controller.program_root()[0] == ProgramRootState.MALFORMED
    assert controller.configured_root() is None
    controller.set_program_root(str(tmp_path))
    assert controller.program_root() == (ProgramRootState.CONFIGURED, tmp_path)
    assert controller.configured_root() == tmp_path


def test_probe_program_root(tmp_path):
    assert probe_program_root(tmp_path / "missing") == ProgramRootProbeResult.UNREACHABLE
    assert probe_program_root(tmp_path) == ProgramRootProbeResult.REACHABLE_BUT_NO_JOB_FOLDERS
    (tmp_path / "B123456").mkdir()
    assert probe_program_root(tmp_path) == ProgramRootProbeResult.REACHABLE


def test_settings_dialog_edits_the_program_folder(qtbot, tmp_path):
    from cockpit.ui.font_scale_controller import FontScaleController
    from cockpit.ui.theme import Theme
    from cockpit.ui.ui_prefs import StyleController
    from cockpit.ui.widgets.settings_dialog import SettingsDialog
    from tests.ui.widgets.test_phase48_settings_dialog import DUMMY_STRUCTURAL_DATA

    theme = Theme.for_testing(
        application={"font_scale": {"default_pt": 10, "min_pt": 8, "max_pt": 24, "step_pt": 1}},
        **DUMMY_STRUCTURAL_DATA,
    )
    app = QApplication.instance()
    settings = ini(tmp_path, "ui.ini")
    controller = ProgramRootController(ini(tmp_path, "roots.ini"))
    dialog = SettingsDialog(StyleController(app, theme, settings), FontScaleController(app, theme, settings),
                            None, None, None, program_root_controller=controller)
    qtbot.addWidget(dialog)
    titles = [g.title() for g in dialog.findChildren(QGroupBox)]
    assert "HLC Program Folder" in titles
    edit = dialog.findChild(QLineEdit, "program_root_edit")
    edit.setText(str(tmp_path))
    assert controller.configured_root() == tmp_path


# --- actions menu and wiring --------------------------------------------------------

def make_bar(qtbot, tmp_path, source_root=True, part_number=JOB):
    controller = SourceRootController(ini(tmp_path, "root.ini"))
    if source_root:
        controller.set_source_root(str(tmp_path))
    bar = AuditActionsBar(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                          source_root_controller=controller)
    qtbot.addWidget(bar)
    session = MagicMock()
    session.current_view.return_value = SimpleNamespace(
        audit_id=11, part_number=part_number, has_pdf=True, has_secondary_pdf=False, status="Not Clear")
    bar.bind(session)
    bar._rebuild_actions_menu()
    return bar


def aoi_action(bar):
    return next(a for a in bar.actions_menu.actions() if a.text() == "AOI XY…")


def test_actions_menu_offers_aoi_xy(qtbot, tmp_path):
    bar = make_bar(qtbot, tmp_path)
    action = aoi_action(bar)
    assert action.isEnabled()
    with qtbot.waitSignal(bar.aoi_xy_requested) as blocker:
        action.trigger()
    assert blocker.args == [11]


def test_actions_menu_explains_why_aoi_xy_is_unavailable(qtbot, tmp_path):
    # pytest-qt holds widgets weakly; keep each bar alive while its actions are read.
    no_root = make_bar(qtbot, tmp_path, source_root=False)
    assert not aoi_action(no_root).isEnabled()
    not_a_job = make_bar(qtbot, tmp_path, part_number="PN-123")
    disabled = aoi_action(not_a_job)
    assert not disabled.isEnabled()
    assert "not a job number" in disabled.toolTip()


def test_audit_view_relays_the_request(qtbot):
    from cockpit.ui.theme import ThemeLoader
    from cockpit.ui.widgets.audit_view import AuditView

    ui_dir = pathlib.Path(__file__).resolve().parents[3] / "cockpit" / "ui"
    theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
    view = AuditView(Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), theme=theme)
    qtbot.addWidget(view)
    with qtbot.waitSignal(view.aoi_xy_requested) as blocker:
        view._actions_bar.aoi_xy_requested.emit(5)
    assert blocker.args == [5]


def _window_stub(tmp_path, job, program_root=True):
    source = SourceRootController(ini(tmp_path, "root.ini"))
    source.set_source_root(str(job.source_root))
    programs = ProgramRootController(ini(tmp_path, "programs.ini"))
    if program_root:
        programs.set_program_root(str(job.program_root))
    bom_row = SimpleNamespace(file_category="BOM", local_storage_path=job.stored_bom)
    stub = SimpleNamespace(
        _source_root_controller=source,
        _program_root_controller=programs,
        _settings=ini(tmp_path, "ui.ini"),
        _bootstrapped=SimpleNamespace(
            audit_repo=SimpleNamespace(find_by_id=lambda audit_id: SimpleNamespace(part_number=job.job)),
            source_file_repo=SimpleNamespace(list_for_audit=lambda audit_id: [bom_row]),
            config=SimpleNamespace(file_storage_root=tmp_path / "uploads"),
        ),
        failures=[],
    )
    stub._on_failed = stub.failures.append
    return stub


def test_main_window_opens_the_dialog_with_both_roots(tmp_path, monkeypatch):
    job = build_job(tmp_path)
    opened = []

    class FakeDialog:
        def __init__(self, **kwargs):
            opened.append(kwargs)

        def exec(self):
            return 0

    monkeypatch.setattr(dialog_module, "AoiXyDialog", FakeDialog)
    stub = _window_stub(tmp_path, job)
    MainWindow._on_aoi_xy_requested(stub, 11)
    assert len(opened) == 1
    kwargs = opened[0]
    assert kwargs["job_number"] == JOB
    assert kwargs["stored_bom"] == job.stored_bom
    assert kwargs["program_root"] == job.program_root
    assert kwargs["forbidden_roots"] == (tmp_path / "uploads",)


def test_main_window_reports_a_missing_bom(tmp_path, monkeypatch):
    job = build_job(tmp_path)
    job.stored_bom.unlink()
    monkeypatch.setattr(dialog_module, "AoiXyDialog", lambda **kwargs: pytest.fail("dialog opened"))
    stub = _window_stub(tmp_path, job)
    MainWindow._on_aoi_xy_requested(stub, 11)
    assert [p.reason_code for p in stub.failures] == ["AOI_BOM_UNAVAILABLE"]


# --- the dialog ------------------------------------------------------------------------

def test_side_from_file_name():
    assert side_from_file_name("T-B123456-XY.txt") is BoardSide.TOP
    assert side_from_file_name("b-B123456-XY.txt") is BoardSide.BOTTOM
    assert side_from_file_name("B123456.txt") is None


def test_dialog_prepares_and_summarises(qtbot, tmp_path):
    dialog = open_dialog(qtbot, build_job(tmp_path), ScriptedPrompts())
    assert dialog.plan is not None
    assert dialog.top_check.isChecked() and dialog.bottom_check.isChecked()
    assert dialog.bottom_start_spin.value() == 4
    text = dialog.summary.toPlainText()
    assert "Top: 9 parts, 3 fiducials" in text
    assert "SMT program origin" in text
    assert "WARNING" not in text
    assert dialog.export_btn.isEnabled() and dialog.check_btn.isEnabled()


def test_dialog_exports_then_replaces_with_confirmation(qtbot, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    settings = ini(tmp_path, "ui.ini")
    prompts = ScriptedPrompts(directory=out)
    dialog = open_dialog(qtbot, build_job(tmp_path), prompts, settings=settings)
    click_and_wait(qtbot, dialog, dialog.export_btn)
    assert sorted(p.name for p in dialog.last_write.written) == [f"B-{JOB}-XY.txt", f"T-{JOB}-XY.txt"]
    assert settings.value(dialog_module.LAST_EXPORT_FOLDER_KEY) == str(out)

    click_and_wait(qtbot, dialog, dialog.export_btn)
    qtbot.waitUntil(lambda: dialog.last_write is not None and len(dialog.last_write.replaced) == 2, timeout=10_000)
    assert ("confirm", "Replace AOI files?") in prompts.asked


def test_dialog_declined_overwrite_writes_nothing(qtbot, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / f"T-{JOB}-XY.txt").write_text("keep me")
    dialog = open_dialog(qtbot, build_job(tmp_path), ScriptedPrompts(directory=out, confirm=False))
    click_and_wait(qtbot, dialog, dialog.export_btn)
    assert dialog.status_label.text() == "Nothing was written."
    assert (out / f"T-{JOB}-XY.txt").read_text() == "keep me"


def test_dialog_exports_only_the_ticked_side(qtbot, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    dialog = open_dialog(qtbot, build_job(tmp_path), ScriptedPrompts(directory=out))
    dialog.bottom_check.setChecked(False)
    click_and_wait(qtbot, dialog, dialog.export_btn)
    assert [p.name for p in out.iterdir()] == [f"T-{JOB}-XY.txt"]


def test_dialog_check_reports_identical(qtbot, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    prompts = ScriptedPrompts(directory=out)
    dialog = open_dialog(qtbot, build_job(tmp_path), prompts)
    click_and_wait(qtbot, dialog, dialog.export_btn)
    prompts.files = [out / f"T-{JOB}-XY.txt", out / f"B-{JOB}-XY.txt", out / "unrelated.txt"]
    click_and_wait(qtbot, dialog, dialog.check_btn)
    assert len(dialog.last_comparisons) == 2
    assert all(c.is_identical for c in dialog.last_comparisons)
    assert "unrelated.txt" in dialog.summary.toPlainText()


def test_dialog_asks_which_xy_file(qtbot, tmp_path):
    job = build_job(tmp_path)
    second = write_xyrs(job.job_dir / "z_second_XYRS.csv", TOP_ROWS)
    prompts = ScriptedPrompts(items=[second.name])
    dialog = open_dialog(qtbot, job, prompts)
    qtbot.waitUntil(lambda: dialog.plan is not None, timeout=10_000)
    assert dialog.plan.xy_path == second
    assert dialog.plan.bottom is None


def test_dialog_asks_which_article(qtbot, tmp_path):
    prompts = ScriptedPrompts(items=["2ND"])
    dialog = open_dialog(qtbot, build_job(tmp_path, article_copy=True), prompts)
    qtbot.waitUntil(lambda: dialog.plan is not None, timeout=10_000)
    assert dialog.plan.top.frame.offset_x_mil == -2000.0
    assert not dialog.bottom_check.isChecked()


def test_dialog_without_a_program_folder_warns(qtbot, tmp_path):
    dialog = open_dialog(qtbot, build_job(tmp_path), ScriptedPrompts(), program_root=None)
    text = dialog.summary.toPlainText()
    assert "HLC program folder is not set" in text
    assert "WARNING Origin" in text
    assert "WARNING Fiducials: from the XY file" in text


def test_dialog_shows_unusable_xy_rows(qtbot, tmp_path):
    job = build_job(tmp_path)
    write_xyrs(job.xy_path, TOP_ROWS, extra_lines=["R77,P,abc,1,0,T,d"])
    dialog = open_dialog(qtbot, job, ScriptedPrompts())
    assert dialog.plan is None
    assert dialog.status_label.text() == "XY file has unusable rows"
    assert "R77" in dialog.summary.toPlainText()
    assert not dialog.export_btn.isEnabled()


def test_dialog_refuses_managed_storage(qtbot, tmp_path):
    storage = tmp_path / "uploads"
    storage.mkdir()
    dialog = open_dialog(qtbot, build_job(tmp_path), ScriptedPrompts(directory=storage), forbidden=(storage,))
    click_and_wait(qtbot, dialog, dialog.export_btn)
    assert dialog.status_label.text() == "Folder not allowed"
    assert list(storage.iterdir()) == []


def test_closing_mid_preparation_is_safe(qtbot, tmp_path):
    job = build_job(tmp_path)
    dialog = AoiXyDialog(job_number=job.job, stored_bom=job.stored_bom, source_root=job.source_root,
                         program_root=job.program_root, prompts=ScriptedPrompts())
    qtbot.addWidget(dialog)
    dialog.reject()
    qtbot.waitUntil(lambda: not _IN_FLIGHT_PROBES, timeout=10_000)
