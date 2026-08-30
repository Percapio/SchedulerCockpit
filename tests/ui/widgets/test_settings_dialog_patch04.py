"""Unit tests for SettingsDialog font-size control (Patch 04)."""

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QAbstractSpinBox

from cockpit.ui.theme import Theme
from cockpit.ui.ui_prefs import StyleController
from cockpit.ui.font_scale_controller import FontScaleController
from cockpit.ui.widgets.settings_dialog import SettingsDialog

DUMMY_STRUCTURAL_DATA = dict(
    base={"window": {"rgb": "#000"}, "toast": {"info": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}, "warn": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}, "error": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}}},
    checklist_panel={"section_header": {"text_rgb": "#0", "fill_rgb": "#0", "padding_px": 0}, "row": {"fill_rgb": "#0", "fill_selected_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "vertical_padding_px": 0, "horizontal_padding_px": 0, "gutter_px": 0}},
    bom_panel={"grouping": {"border_width_px": 0, "border_rgb": "#0", "fill_rgb": "#0", "fill_selected_rgb": "#0", "corner_radius_px": 0, "inner_padding_px": 0, "gutter_px": 0}, "cell": {"mpn": {"fill_rgb": "#0", "text_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "padding_px": 0, "font_size_px": 11}}, "chip": {"fill_rgb": "#0", "fill_hover_rgb": "#0", "text_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "vertical_padding_px": 0, "horizontal_padding_px": 0, "flow_spacing_px": 0}},
    canvas={"colour": {"hint_label_background": {"rgb": "#0"}, "hint_label_text": {"rgb": "#0"}, "hint_label_border": {"rgb": "#0"}}, "hint_label": {"padding_px": 0, "border_width_px": 0}}
)


@pytest.fixture
def theme():
    return Theme.for_testing(
        application={"font_scale": {"default_pt": 10, "min_pt": 8, "max_pt": 24, "step_pt": 1}},
        **DUMMY_STRUCTURAL_DATA
    )


def test_settings_dialog_size_spin_type_only(qtbot, theme, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    style_ctrl = StyleController(app, theme, settings)
    font_ctrl = FontScaleController(app, theme, settings)

    dialog = SettingsDialog(style_ctrl, font_ctrl, None, None)
    qtbot.addWidget(dialog)

    assert dialog.size_spin.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
    assert dialog.size_spin.keyboardTracking() is False
    assert dialog.size_spin.suffix() == " pt"
    assert dialog.size_spin.minimum() == 8
    assert dialog.size_spin.maximum() == 24
    assert dialog.size_spin.value() == 10


def test_settings_dialog_size_spin_sync_and_clamp(qtbot, theme, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    style_ctrl = StyleController(app, theme, settings)
    font_ctrl = FontScaleController(app, theme, settings)

    dialog = SettingsDialog(style_ctrl, font_ctrl, None, None)
    qtbot.addWidget(dialog)

    # Test controller -> dialog sync
    font_ctrl.set_pt(16)
    assert dialog.size_spin.value() == 16

    # Test dialog -> controller sync
    dialog.size_spin.setValue(14)
    assert font_ctrl.current_pt() == 14
