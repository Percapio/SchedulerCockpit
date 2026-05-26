import pytest
from cockpit.ui.theme import Theme, ConfigurationError, ThemeLoader

def test_qss_font_scaling():
    theme = Theme.for_testing(
        application={"font_scale": {"default_pt": 10, "min_pt": 8, "max_pt": 24, "step_pt": 1}},
        base={"window": {"rgb": "#000"}, "toast": {"info": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}, "warn": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}, "error": {"background_rgb": "#0", "text_rgb": "#0", "border_rgb": "#0"}}},
        left_panel={"section_header": {"text_rgb": "#0", "fill_rgb": "#0", "padding_px": 0}, "row": {"fill_rgb": "#0", "fill_selected_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "vertical_padding_px": 0, "horizontal_padding_px": 0, "gutter_px": 0}, "ship_date_field": {"text_rgb": "#0", "fill_rgb": "#0", "border_rgb": "#0"}},
        bom_panel={"grouping": {"border_width_px": 0, "border_rgb": "#0", "fill_rgb": "#0", "fill_selected_rgb": "#0", "corner_radius_px": 0, "inner_padding_px": 0, "gutter_px": 0}, "cell": {"mpn": {"fill_rgb": "#0", "text_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "padding_px": 0, "font_size_px": 11}}, "chip": {"fill_rgb": "#0", "fill_hover_rgb": "#0", "text_rgb": "#0", "text_selected_rgb": "#0", "corner_radius_px": 0, "vertical_padding_px": 0, "horizontal_padding_px": 0, "flow_spacing_px": 0}},
        canvas={"colour": {"hint_label_background": {"rgb": "#0"}, "hint_label_text": {"rgb": "#0"}, "hint_label_border": {"rgb": "#0"}}, "hint_label": {"padding_px": 0, "border_width_px": 0}}
    )
    
    qss_10 = theme.qss(10)
    qss_12 = theme.qss(12)
    
    assert "font-size: 24px;" in qss_10
    # 24 * 1.2 = 28.8 => 29
    assert "font-size: 29px;" in qss_12
    
    assert "font-size: 16px;" in qss_10
    # 16 * 1.2 = 19.2 => 19
    assert "font-size: 19px;" in qss_12
    
    # BOM cell scaling
    assert "font-size: 11px;" in qss_10
    # 11 * 1.2 = 13.2 => 13
    assert "font-size: 13px;" in qss_12

def test_structural_invariant_font_scale_bounds():
    data = {
        "application": {"font_scale": {"default_pt": 20, "min_pt": 8, "max_pt": 15, "step_pt": 1}},
        "canvas": {"z_order": {"base_pixmap": 0, "dim": 1, "highlight": 2}, "scalar": {"highlight_scale": 1.5}, "zoom": {"min_scale": 0.5, "max_scale": 2.0, "step": 1.5, "render_multiplier": 1.0}, "colour": {}}
    }
    with pytest.raises(ConfigurationError, match="must be within min_pt and max_pt"):
        ThemeLoader.validate_structural_invariants(data)
