import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from cockpit.ui.theme import Theme
from cockpit.ui.font_scale_controller import FontScaleController

DUMMY_STRUCTURAL_DATA = dict(
    base={"window": {"rgb": "#000"}, "toast": {"info": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}, "warn": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}, "error": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}}},
    left_panel={"section_header": {"text_rgb": "#0", "fill_rgb": "#0", "padding_px": 0}, "row": {"fill_rgb": "#0", "fill_selected_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "vertical_padding_px": 0, "horizontal_padding_px": 0, "gutter_px": 0}, "ship_date_field": {"text_rgb": "#0", "fill_rgb": "#0", "border_rgb": "#0"}},
    bom_panel={"grouping": {"border_width_px": 0, "border_rgb": "#0", "fill_rgb": "#0", "fill_selected_rgb": "#0", "corner_radius_px": 0, "inner_padding_px": 0, "gutter_px": 0}, "cell": {"mpn": {"fill_rgb": "#0", "text_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "padding_px": 0, "font_size_px": 11}}, "chip": {"fill_rgb": "#0", "fill_hover_rgb": "#0", "text_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "vertical_padding_px": 0, "horizontal_padding_px": 0, "flow_spacing_px": 0}},
    canvas={"colour": {"hint_label_background": {"rgb": "#0"}, "hint_label_text": {"rgb": "#0"}, "hint_label_border": {"rgb": "#0"}}, "hint_label": {"padding_px": 0, "border_width_px": 0}}
)

@pytest.fixture
def theme():
    return Theme.for_testing(
        application={"font_scale": {"default_pt": 10, "min_pt": 8, "max_pt": 24, "step_pt": 1}},
        **DUMMY_STRUCTURAL_DATA
    )

@pytest.fixture
def theme_step_2():
    return Theme.for_testing(
        application={"font_scale": {"default_pt": 10, "min_pt": 8, "max_pt": 24, "step_pt": 2}},
        **DUMMY_STRUCTURAL_DATA
    )

def test_request_delta_positive(qtbot, theme, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    controller = FontScaleController(app, theme, settings)
    
    with qtbot.waitSignal(controller.scale_changed) as blocker:
        controller.request_delta(1)
        
    QApplication.processEvents()
        
    assert blocker.args == [11]
    assert controller.current_pt() == 11
    assert settings.value("audit_view/font_scale_pt") == 11
    assert "font-size:" in app.styleSheet()

def test_request_delta_max_clamp_noop(qtbot, theme, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("audit_view/font_scale_pt", 24)
    controller = FontScaleController(app, theme, settings)
    
    controller.request_delta(1)
    
    # Value should remain 24, no signal emitted (qtbot would timeout if we waited, so we just check state)
    assert controller.current_pt() == 24

def test_request_delta_min_clamp_noop(qtbot, theme, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("audit_view/font_scale_pt", 8)
    controller = FontScaleController(app, theme, settings)
    
    controller.request_delta(-1)
    assert controller.current_pt() == 8

def test_request_delta_step_arithmetic(qtbot, theme, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    controller = FontScaleController(app, theme, settings)
    
    controller.request_delta(2)
    assert controller.current_pt() == 12

def test_request_delta_step_2_arithmetic(qtbot, theme_step_2, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    controller = FontScaleController(app, theme_step_2, settings)
    
    controller.request_delta(1)
    assert controller.current_pt() == 12

def test_persisted_value_outside_bounds_fallback(qtbot, theme, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("audit_view/font_scale_pt", 30) # outside [8, 24]
    
    controller = FontScaleController(app, theme, settings)
    
    assert controller.current_pt() == 10
    # verify settings were overwritten
    assert settings.value("audit_view/font_scale_pt") == 10

def test_persisted_value_missing_returns_default(qtbot, theme, tmp_path):
    app = QApplication.instance()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    
    controller = FontScaleController(app, theme, settings)
    assert controller.current_pt() == 10
