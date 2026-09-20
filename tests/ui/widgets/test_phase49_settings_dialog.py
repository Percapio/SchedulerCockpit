"""Phase 49 section 8.3 — the alpha gate over the credential block."""

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from cockpit.services.second_ops import (
    SECOND_OPS_TERMS_KEY,
    SHIPPED_TERMS_BY_GENERATION,
    SecondOpsSettingsController,
)
from cockpit.settings.mpn_library import MpnLibrarySettingsController
from cockpit.ui.font_scale_controller import FontScaleController
from cockpit.ui.theme import Theme
from cockpit.ui.ui_prefs import StyleController
from cockpit.ui.widgets.settings_dialog import SettingsDialog

from tests.ui.widgets.test_phase48_settings_dialog import DUMMY_STRUCTURAL_DATA


@pytest.fixture
def theme():
    return Theme.for_testing(
        application={"font_scale": {"default_pt": 10, "min_pt": 8, "max_pt": 24, "step_pt": 1}},
        **DUMMY_STRUCTURAL_DATA
    )


@pytest.fixture
def mpn_controller(tmp_path):
    return MpnLibrarySettingsController(
        QSettings(str(tmp_path / "mpn.ini"), QSettings.Format.IniFormat)
    )


@pytest.fixture
def make_dialog(qtbot, theme, tmp_path, mpn_controller):
    def build(second_ops_controller=None):
        app = QApplication.instance()
        settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
        dialog = SettingsDialog(
            StyleController(app, theme, settings),
            FontScaleController(app, theme, settings),
            None, second_ops_controller, None,
            mpn_library_controller=mpn_controller,
        )
        qtbot.addWidget(dialog)
        return dialog

    return build


def test_group_is_labelled_alpha(make_dialog):
    from PyQt6.QtWidgets import QGroupBox

    dialog = make_dialog()
    titles = [g.title() for g in dialog.findChildren(QGroupBox)]
    assert "MPN Library (Alpha)" in titles


def test_credential_block_hidden_while_the_gate_is_off(make_dialog, mpn_controller):
    assert mpn_controller.is_enabled() is False
    dialog = make_dialog()
    dialog.show()
    assert dialog._credential_block.isVisible() is False


def test_credential_block_visible_when_the_gate_is_on(make_dialog, mpn_controller):
    mpn_controller.set_enabled(True)
    dialog = make_dialog()
    dialog.show()
    assert dialog._credential_block.isVisible() is True


def test_toggling_the_gate_moves_the_block_both_ways(make_dialog, mpn_controller):
    dialog = make_dialog()
    dialog.show()

    dialog.set_credential_block_visible(True)
    assert dialog._credential_block.isVisible() is True

    dialog.set_credential_block_visible(False)
    assert dialog._credential_block.isVisible() is False


def test_hiding_mid_probe_tears_down_and_re_renders(make_dialog, mpn_controller):
    """Teardown alone leaves both buttons disabled on stale 'Checking...' text."""
    mpn_controller.set_enabled(True)
    mpn_controller.set_digikey_credentials("id-1", "secret-1")
    dialog = make_dialog()
    dialog.show()

    dialog._probe_in_flight = True
    dialog._refresh_mpn_credential_state()
    assert dialog._test_connection_btn.isEnabled() is False
    assert dialog._mpn_status_lbl.text() == "Checking..."

    dialog.set_credential_block_visible(False)

    assert dialog._probe_in_flight is False
    assert dialog._probe_worker is None
    assert dialog._test_connection_btn.isEnabled() is True
    assert dialog._mpn_status_lbl.text() != "Checking..."


def test_terms_edit_reflects_a_migrated_vocabulary(make_dialog, tmp_path):
    settings = QSettings(str(tmp_path / "ops.ini"), QSettings.Format.IniFormat)
    settings.setValue(SECOND_OPS_TERMS_KEY, "Fuse, Shunt")
    controller = SecondOpsSettingsController(settings)
    controller.migrate_shipped_terms()

    dialog = make_dialog(second_ops_controller=controller)

    rendered = dialog.terms_edit.text()
    assert rendered.startswith("Fuse, Shunt")
    assert "LOCTITE" in rendered
    # The store held 2 of the 7 generation-1 terms; the other 5 were deleted by
    # the operator and must stay deleted. 2 kept + 19 generation-2 terms.
    assert len(rendered.split(",")) == 2 + len(SHIPPED_TERMS_BY_GENERATION[2])
    assert "SHNT" not in rendered
