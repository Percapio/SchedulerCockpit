"""Phase 48 section 12.5 -- the credentials group box.

The rules in sections 4.2 and 10.2 are containment rules with a UI that can
break them, so each is asserted here rather than left as prose.
"""

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox

from cockpit.settings.mpn_library import MpnLibrarySettingsController
from cockpit.ui.font_scale_controller import FontScaleController
from cockpit.ui.theme import Theme
from cockpit.ui.ui_prefs import StyleController
from cockpit.ui.widgets.settings_dialog import SettingsDialog

DUMMY_STRUCTURAL_DATA = dict(
    base={"window": {"rgb": "#000"}, "toast": {"info": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}, "warn": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}, "error": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}}},
    checklist_panel={"section_header": {"text_rgb": "#0", "fill_rgb": "#0", "padding_px": 0}, "row": {"fill_rgb": "#0", "fill_selected_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "vertical_padding_px": 0, "horizontal_padding_px": 0, "gutter_px": 0}},
    bom_panel={"grouping": {"border_width_px": 0, "border_rgb": "#0", "fill_rgb": "#0", "fill_selected_rgb": "#0", "corner_radius_px": 0, "inner_padding_px": 0, "gutter_px": 0}, "cell": {"mpn": {"fill_rgb": "#0", "text_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "padding_px": 0, "font_size_px": 11}}, "chip": {"fill_rgb": "#0", "fill_hover_rgb": "#0", "text_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "vertical_padding_px": 0, "horizontal_padding_px": 0, "flow_spacing_px": 0}},
    canvas={"colour": {"hint_label_background": {"rgb": "#0"}, "hint_label_text": {"rgb": "#0"}, "hint_label_border": {"rgb": "#0"}}, "hint_label": {"padding_px": 0, "border_width_px": 0}}
)

IMPROBABLE_SECRET = "zzq-secret-8f3a1c07-never-rendered"


@pytest.fixture
def theme():
    return Theme.for_testing(
        application={"font_scale": {"default_pt": 10, "min_pt": 8, "max_pt": 24, "step_pt": 1}},
        **DUMMY_STRUCTURAL_DATA
    )


@pytest.fixture
def controller(tmp_path):
    return MpnLibrarySettingsController(
        QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )


@pytest.fixture
def make_dialog(qtbot, theme, tmp_path, controller):
    def build(enrichment_in_flight=None):
        app = QApplication.instance()
        settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
        dialog = SettingsDialog(
            StyleController(app, theme, settings),
            FontScaleController(app, theme, settings),
            None, None, None,
            mpn_library_controller=controller,
            enrichment_in_flight=enrichment_in_flight,
        )
        qtbot.addWidget(dialog)
        return dialog

    return build


@pytest.fixture
def accept_forget(monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok
    )


# --- section 4.2, the never-populated secret field --------------------------

def test_stored_secret_is_never_read_back_into_the_widget(make_dialog, controller):
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    dialog = make_dialog()

    assert dialog._client_secret_edit.text() == ""
    assert "stored" in dialog._client_secret_edit.placeholderText()
    assert dialog._client_id_edit.text() == "id-1"


def test_secret_field_uses_password_echo(make_dialog):
    dialog = make_dialog()
    assert dialog._client_secret_edit.echoMode() == QLineEdit.EchoMode.Password


def test_placeholder_says_when_no_secret_is_stored(make_dialog):
    dialog = make_dialog()
    assert dialog._client_secret_edit.placeholderText() == "No secret stored."


def test_plaintext_disclosure_is_present_with_the_fields(make_dialog):
    dialog = make_dialog()
    assert dialog._plaintext_disclosure_lbl is not None
    assert "unencrypted" in dialog._plaintext_disclosure_lbl.text()


# --- section 4.3, the commit table ------------------------------------------

def test_typing_writes_nothing_until_commit(make_dialog, controller):
    from cockpit.settings.mpn_library import Unset

    dialog = make_dialog()
    dialog._client_id_edit.setText("id-1")
    dialog._client_secret_edit.setText(IMPROBABLE_SECRET)

    assert isinstance(controller.digikey_credentials(), Unset)


def test_both_fields_filled_commits_the_pair(make_dialog, controller):
    from cockpit.settings.mpn_library import Configured

    dialog = make_dialog()
    dialog._client_id_edit.setText("id-1")
    dialog._client_secret_edit.setText(IMPROBABLE_SECRET)
    dialog._commit_digikey_credentials()

    stored = controller.digikey_credentials()
    assert isinstance(stored, Configured)
    assert stored.credentials.client_secret == IMPROBABLE_SECRET


def test_id_only_with_no_stored_secret_writes_nothing(make_dialog, controller):
    """The first-run case. The tempting implementation writes the id, leaving
    storage in a state the controller is specified never to reach."""
    from cockpit.settings.mpn_library import Unset

    dialog = make_dialog()
    dialog._client_id_edit.setText("id-1")
    dialog._commit_digikey_credentials()

    assert isinstance(controller.digikey_credentials(), Unset)
    assert dialog._client_id_edit.text() == "id-1"


def test_id_only_with_a_stored_secret_commits_the_id_alone(make_dialog, controller):
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    dialog = make_dialog()
    dialog._client_id_edit.setText("id-2")
    dialog._commit_digikey_credentials()

    stored = controller.digikey_credentials()
    assert stored.credentials.client_id == "id-2"
    assert stored.credentials.client_secret == IMPROBABLE_SECRET


def test_secret_only_writes_nothing_and_keeps_the_typed_value(make_dialog, controller):
    from cockpit.settings.mpn_library import Unset

    dialog = make_dialog()
    dialog._client_secret_edit.setText(IMPROBABLE_SECRET)
    dialog._commit_digikey_credentials()

    assert isinstance(controller.digikey_credentials(), Unset)
    assert dialog._client_secret_edit.text() == IMPROBABLE_SECRET


# --- section 4.4, the state table -------------------------------------------

def test_unset_row(make_dialog):
    dialog = make_dialog()
    assert dialog._test_connection_btn.isEnabled() is False
    assert dialog._forget_credentials_btn.isEnabled() is False
    assert dialog._mpn_status_lbl.text() == "No credentials stored."


def test_partial_row_names_the_missing_field_and_allows_forget(make_dialog, controller):
    controller._settings.setValue("library/digikey_client_id", "id-1")
    dialog = make_dialog()

    assert dialog._test_connection_btn.isEnabled() is False
    assert dialog._forget_credentials_btn.isEnabled() is True
    assert "client secret" in dialog._mpn_status_lbl.text()


def test_rejected_base_url_row_names_the_fault(make_dialog, controller):
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    controller.set_api_base_url("http://api.digikey.com")
    dialog = make_dialog()

    assert dialog._test_connection_btn.isEnabled() is False
    assert dialog._forget_credentials_btn.isEnabled() is True
    assert "https" in dialog._mpn_status_lbl.text()


def test_configured_row_names_the_host_that_receives_the_secret(make_dialog, controller):
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    controller.set_api_base_url("https://sandbox-api.digikey.com")
    dialog = make_dialog()

    assert dialog._test_connection_btn.isEnabled() is True
    assert dialog._forget_credentials_btn.isEnabled() is True
    assert "sandbox-api.digikey.com" in dialog._mpn_status_lbl.text()


def test_enrichment_in_flight_disables_forget(make_dialog, controller):
    """An operator who clicks Forget during a run expects the run to stop, and
    it does not. Disabling the button is how the UI avoids implying otherwise."""
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    dialog = make_dialog(enrichment_in_flight=lambda: True)

    assert dialog._forget_credentials_btn.isEnabled() is False
    assert dialog._test_connection_btn.isEnabled() is False
    assert dialog._mpn_status_lbl.text() == "Enrichment running."


def test_probe_in_flight_disables_forget(make_dialog, controller):
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    dialog = make_dialog()
    dialog._probe_in_flight = True
    dialog._refresh_mpn_credential_state()

    assert dialog._forget_credentials_btn.isEnabled() is False
    assert dialog._test_connection_btn.isEnabled() is False
    assert dialog._mpn_status_lbl.text() == "Checking..."


# --- Forget -----------------------------------------------------------------

def test_forget_removes_both_keys_and_leaves_the_toggle_alone(
    make_dialog, controller, accept_forget
):
    from cockpit.settings.mpn_library import Unset

    controller.set_enabled(True)
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    dialog = make_dialog()
    dialog._on_forget_credentials()

    assert isinstance(controller.digikey_credentials(), Unset)
    assert controller.is_enabled() is True
    assert dialog._client_id_edit.text() == ""
    assert dialog._mpn_status_lbl.text() == "No credentials stored."


# --- section 10.2, closing --------------------------------------------------

def test_close_clears_the_credential_widgets(make_dialog, controller):
    dialog = make_dialog()
    dialog._client_id_edit.setText("id-1")
    dialog._client_secret_edit.setText(IMPROBABLE_SECRET)

    dialog.done(QDialog.DialogCode.Accepted)

    assert dialog._client_id_edit.text() == ""
    assert dialog._client_secret_edit.text() == ""


def test_accept_commits_what_the_widgets_held(make_dialog, controller):
    from cockpit.settings.mpn_library import Configured

    dialog = make_dialog()
    dialog._client_id_edit.setText("id-1")
    dialog._client_secret_edit.setText(IMPROBABLE_SECRET)

    dialog.done(QDialog.DialogCode.Accepted)

    stored = controller.digikey_credentials()
    assert isinstance(stored, Configured)
    assert stored.credentials.client_secret == IMPROBABLE_SECRET


def test_reject_commits_nothing(make_dialog, controller):
    from cockpit.settings.mpn_library import Unset

    dialog = make_dialog()
    dialog._client_id_edit.setText("id-1")
    dialog._client_secret_edit.setText(IMPROBABLE_SECRET)

    dialog.done(QDialog.DialogCode.Rejected)

    assert isinstance(controller.digikey_credentials(), Unset)


# --- section 5.3, abandonment rather than cancellation ----------------------

@pytest.fixture
def blocking_probe(monkeypatch):
    """Replaces the probe with one that hangs until the test releases it.

    A socket read is not interruptible from the UI thread, so the hang is what
    the close path and the watchdog both have to survive.
    """
    import threading

    from cockpit.services.mpn_library.credential_probe import CredentialProbeResult
    from cockpit.ui.widgets import settings_dialog as dialog_module

    release = threading.Event()
    entered = threading.Event()

    def hanging_probe(credentials, api_base, clock):
        entered.set()
        release.wait(timeout=10)
        return CredentialProbeResult.REACHABLE

    monkeypatch.setattr(dialog_module, "probe_digikey_credentials", hanging_probe)
    yield release, entered
    release.set()


def _drain_probes(qtbot):
    from cockpit.ui.widgets.settings_dialog import _IN_FLIGHT_PROBES
    qtbot.waitUntil(lambda: not _IN_FLIGHT_PROBES, timeout=5000)


def test_closing_during_a_probe_neither_crashes_nor_blocks(
    make_dialog, controller, blocking_probe, qtbot
):
    import time

    from cockpit.ui.widgets.settings_dialog import _IN_FLIGHT_PROBES

    release, entered = blocking_probe
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    dialog = make_dialog()

    dialog._on_test_connection()
    qtbot.waitUntil(entered.is_set, timeout=5000)
    assert dialog._probe_in_flight is True

    started_at = time.monotonic()
    dialog.done(QDialog.DialogCode.Rejected)
    elapsed = time.monotonic() - started_at

    # The close never waits on the thread: wait() would block the UI thread for
    # exactly as long as the read, which is the freeze the watchdog exists for.
    assert elapsed < 1.0
    assert dialog._probe_worker is None
    assert dialog._probe_result_handler is None

    release.set()
    _drain_probes(qtbot)
    assert _IN_FLIGHT_PROBES == set()


def test_a_result_arriving_after_its_dialog_is_gone_is_discarded(
    make_dialog, controller, blocking_probe, qtbot
):
    from cockpit.ui.widgets.settings_dialog import _IN_FLIGHT_PROBES

    release, entered = blocking_probe
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    dialog = make_dialog()

    dialog._on_test_connection()
    qtbot.waitUntil(entered.is_set, timeout=5000)
    dialog.done(QDialog.DialogCode.Rejected)

    release.set()
    _drain_probes(qtbot)

    # A disconnected signal has nothing to deliver. Reaching a deleted widget
    # here would abort the process rather than fail the assertion.
    assert dialog._probe_in_flight is False


def test_two_probes_in_flight_are_both_referenced(
    make_dialog, controller, blocking_probe, qtbot
):
    from cockpit.ui.widgets.settings_dialog import _IN_FLIGHT_PROBES

    release, entered = blocking_probe
    controller.set_digikey_credentials("id-1", IMPROBABLE_SECRET)
    dialog = make_dialog()

    dialog._on_test_connection()
    qtbot.waitUntil(entered.is_set, timeout=5000)
    first_worker = dialog._probe_worker

    # What the watchdog does: abandon the first probe, leaving the operator
    # free to click again while it is still reading.
    dialog._probe_in_flight = False
    dialog._probe_worker = None
    dialog._probe_result_handler = None

    dialog._on_test_connection()
    second_worker = dialog._probe_worker

    assert first_worker is not second_worker
    assert len(_IN_FLIGHT_PROBES) == 2

    release.set()
    _drain_probes(qtbot)
