"""Patch 11 sections 5.1 and 8.1 — the source-root probe.

The three tests mirrored from Phase 48 are the ones that matter: before this
patch the source-root probe had no watchdog, a completion handler that outlived
its widgets, and single-slot thread attributes. Two of those three terminated
the process, so a pass here is the assertion.
"""

import threading
import time

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QDialog, QGroupBox, QLabel, QPushButton

from cockpit.settings.source_root import RootProbeResult, SourceRootController
from cockpit.ui.font_scale_controller import FontScaleController
from cockpit.ui.theme import Theme
from cockpit.ui.ui_prefs import StyleController
from cockpit.ui.widgets import settings_dialog as dialog_module
from cockpit.ui.widgets.qt_lifecycle import _ADMISSIONS, _IN_FLIGHT_PROBES
from cockpit.ui.widgets.settings_dialog import SettingsDialog

from tests.ui.widgets.test_phase48_settings_dialog import DUMMY_STRUCTURAL_DATA


@pytest.fixture(autouse=True)
def _clean_registries():
    _IN_FLIGHT_PROBES.clear()
    _ADMISSIONS.clear()
    yield
    _IN_FLIGHT_PROBES.clear()
    _ADMISSIONS.clear()


@pytest.fixture
def theme():
    return Theme.for_testing(
        application={"font_scale": {"default_pt": 10, "min_pt": 8, "max_pt": 24, "step_pt": 1}},
        **DUMMY_STRUCTURAL_DATA
    )


@pytest.fixture
def source_root_controller(tmp_path):
    settings = QSettings(str(tmp_path / "root.ini"), QSettings.Format.IniFormat)
    controller = SourceRootController(settings)
    controller.set_source_root(str(tmp_path))
    return controller


@pytest.fixture
def make_dialog(qtbot, theme, tmp_path, source_root_controller):
    def build():
        app = QApplication.instance()
        settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
        dialog = SettingsDialog(
            StyleController(app, theme, settings),
            FontScaleController(app, theme, settings),
            None, None, None,
            source_root_controller=source_root_controller,
        )
        qtbot.addWidget(dialog)
        return dialog

    return build


@pytest.fixture
def hanging_probe(monkeypatch):
    """A source root that accepts the call and never answers, like a dead share."""
    release = threading.Event()
    entered = threading.Event()

    def blocking_probe(candidate):
        entered.set()
        release.wait(timeout=10)
        return RootProbeResult.REACHABLE

    monkeypatch.setattr(dialog_module, "probe_source_root", blocking_probe, raising=False)
    monkeypatch.setattr(
        "cockpit.settings.source_root.probe_source_root", blocking_probe
    )
    yield release, entered
    release.set()


def _validate_button(dialog) -> QPushButton:
    for group in dialog.findChildren(QGroupBox):
        if group.title() == "Source Root":
            for btn in group.findChildren(QPushButton):
                if btn.text() == "Validate":
                    return btn
    raise AssertionError("Validate button not found")


def _status_label(dialog) -> QLabel:
    for group in dialog.findChildren(QGroupBox):
        if group.title() == "Source Root":
            labels = group.findChildren(QLabel)
            return labels[-1]
    raise AssertionError("status label not found")


def _drain(qtbot):
    qtbot.waitUntil(lambda: not _IN_FLIGHT_PROBES, timeout=5000)


def test_a_reachable_root_reports_itself(make_dialog, qtbot, tmp_path):
    (tmp_path / "B123xxx").mkdir()
    dialog = make_dialog()
    status = _status_label(dialog)

    _validate_button(dialog).click()
    qtbot.waitUntil(lambda: status.text() == "Reachable.", timeout=5000)
    assert _validate_button(dialog).isEnabled()
    _drain(qtbot)


def test_a_root_with_no_job_tree_says_so(make_dialog, qtbot):
    dialog = make_dialog()
    status = _status_label(dialog)

    _validate_button(dialog).click()
    qtbot.waitUntil(
        lambda: status.text() == "Reachable, but no job tree found.", timeout=5000
    )
    _drain(qtbot)


# --- the three mirrored from Phase 48 ---------------------------------------

def test_closing_during_a_probe_neither_crashes_nor_blocks(
    make_dialog, hanging_probe, qtbot
):
    release, entered = hanging_probe
    dialog = make_dialog()

    _validate_button(dialog).click()
    qtbot.waitUntil(entered.is_set, timeout=5000)

    started_at = time.monotonic()
    dialog.done(QDialog.DialogCode.Rejected)
    elapsed = time.monotonic() - started_at

    # Never waits on the thread. wait() would block the UI thread for exactly as
    # long as the share does, which is the freeze the watchdog exists for.
    assert elapsed < 1.0

    release.set()
    _drain(qtbot)
    assert _IN_FLIGHT_PROBES == set()


def test_a_result_arriving_after_its_dialog_is_gone_is_discarded(
    make_dialog, hanging_probe, qtbot
):
    """Before this patch the handler closed over live widgets with no guard.

    Reaching one after the dialog was destroyed raised inside a slot, and PyQt
    terminates on that. A pass here is the absence of an abort.
    """
    release, entered = hanging_probe
    dialog = make_dialog()

    _validate_button(dialog).click()
    qtbot.waitUntil(entered.is_set, timeout=5000)
    dialog.done(QDialog.DialogCode.Rejected)

    release.set()
    _drain(qtbot)


def test_two_probes_in_flight_are_both_referenced(make_dialog, hanging_probe, qtbot):
    """Before this patch the second click collected the first running QThread."""
    release, entered = hanging_probe
    dialog = make_dialog()

    _validate_button(dialog).click()
    qtbot.waitUntil(entered.is_set, timeout=5000)
    first = dialog._source_root_admission

    # What the watchdog does: re-enable the button, leaving the operator free to
    # click again while the first probe is still reading.
    _validate_button(dialog).setEnabled(True)
    _validate_button(dialog).click()
    second = dialog._source_root_admission

    assert first is not second
    assert first.thread is not second.thread
    assert len(_IN_FLIGHT_PROBES) == 2
    assert first.thread.isRunning() and second.thread.isRunning()

    release.set()
    _drain(qtbot)


def test_the_watchdog_reports_and_re_enables_validate(
    make_dialog, hanging_probe, qtbot, monkeypatch
):
    """Before this patch there was no watchdog: Checking... forever."""
    monkeypatch.setattr(dialog_module, "FETCH_WATCHDOG_TIMEOUT_MS", 150)
    release, entered = hanging_probe
    dialog = make_dialog()
    status = _status_label(dialog)

    _validate_button(dialog).click()
    qtbot.waitUntil(entered.is_set, timeout=5000)
    qtbot.waitUntil(lambda: "No answer" in status.text(), timeout=5000)

    assert _validate_button(dialog).isEnabled()

    release.set()
    _drain(qtbot)


def test_the_source_root_probe_uses_the_share_scan_timeout():
    """Not the DigiKey connect timeout, which is sized against HTTPS."""
    from cockpit.ui.widgets.qt_lifecycle import FETCH_WATCHDOG_TIMEOUT_MS

    assert dialog_module.FETCH_WATCHDOG_TIMEOUT_MS == FETCH_WATCHDOG_TIMEOUT_MS
    assert dialog_module.FETCH_WATCHDOG_TIMEOUT_MS != dialog_module.PROBE_WATCHDOG_TIMEOUT_MS


def test_main_window_still_sees_the_constant_under_its_own_name():
    from cockpit.ui.main_window import FETCH_WATCHDOG_TIMEOUT_MS as from_window
    from cockpit.ui.widgets.qt_lifecycle import FETCH_WATCHDOG_TIMEOUT_MS as from_seam

    assert from_window == from_seam == 20_000
