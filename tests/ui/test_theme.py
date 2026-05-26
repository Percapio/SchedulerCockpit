import json
import pathlib
import pytest
from PyQt6.QtGui import QColor, QBrush, QPen
from PyQt6.QtCore import Qt

from cockpit.ui.theme import Theme, ThemeLoader, ConfigurationError

@pytest.fixture
def valid_theme_data():
    return {
        "base": {
            "window": { "rgb": "#FAFAFA" },
            "font": { "family": "Segoe UI", "size_px": 13 },
            "toast": {
                "info":  { "background_rgb": "#E3F2FD", "text_rgb": "#0D47A1", "border_rgb": "#1976D2" },
                "warn":  { "background_rgb": "#FFF3E0", "text_rgb": "#E65100", "border_rgb": "#FF9800" },
                "error": { "background_rgb": "#E8F5E9", "text_rgb": "#2E7D32", "border_rgb": "#4CAF50" }
            }
        },
        "left_panel": {
            "layout": {
                "min_width_px": 280
            },
            "section_header": { "fill_rgb": "#FAFAFA", "text_rgb": "#555555", "padding_px": 4 },
            "row": {
                "fill_rgb": "#2A2A2A", "fill_selected_rgb": "#FFFACD", "text_selected_rgb": "#000000",
                "corner_radius_px": 4, "vertical_padding_px": 6, "horizontal_padding_px": 8,
                "gutter_px": 3
            },
            "ship_date_field": { "fill_rgb": "#FAFAFA", "text_rgb": "#666666", "border_rgb": "#CCCCCC" },
            "progress_view": { "fill_rgb": "#FAFAFA", "text_rgb": "#000000" }
        },
        "canvas": {
            "colour": {
                "highlight_pen": { "rgb": "#FF00FF" },
                "dim_overlay": { "rgb": "#000000", "alpha": 128 },
                "hint_label_background": { "rgb": "#FFFFFF" },
                "hint_label_text": { "rgb": "#000000" },
                "hint_label_border": { "rgb": "#000000" }
            },
            "pen_width": { "highlight": 3 },
            "z_order": { "base_pixmap": 0.0, "dim": 1.0, "highlight": 2.0 },
            "zoom": { "min_scale": 1.0, "max_scale": 8.0, "step": 1.25, "render_multiplier": 3.0 },
            "scalar": { "highlight_scale": 2.0 },
            "hint_label": { "padding_px": 4, "border_width_px": 1 }
        },
        "bom_panel": {
            "layout": {
                "min_width_percent": 0.15,
                "min_width_absolute_px": 200
            },
            "grouping": {
                "border_rgb": "#3F3F3F", "border_width_px": 1, "fill_rgb": "#252525", "fill_selected_rgb": "#FFFACD",
                "corner_radius_px": 4, "inner_padding_px": 6, "gutter_px": 3
            },
            "cell": {
                "mpn": { "fill_rgb": "#2A2A2A", "text_rgb": "#E8E8E8", "text_selected_rgb": "#000000", "corner_radius_px": 4, "padding_px": 6 },
                "refdes": { "fill_rgb": "#2A2A2A", "text_rgb": "#E8E8E8", "text_selected_rgb": "#000000", "corner_radius_px": 4, "padding_px": 6 },
                "description": { "fill_rgb": "#2A2A2A", "text_rgb": "#AAAAAA", "text_selected_rgb": "#000000", "corner_radius_px": 4, "padding_px": 6, "font_size_px": 11 }
            },
            "chip": {
                "fill_rgb": "#3A3A3A", "fill_hover_rgb": "#4A4A4A", "text_rgb": "#E8E8E8", "text_selected_rgb": "#FFFF00",
                "corner_radius_px": 3, "vertical_padding_px": 3, "horizontal_padding_px": 6, "flow_spacing_px": 4
            }
        }
    }


def test_ThemeLoader_ThemePathMissing_RaisesConfigurationErrorThemeFileNotFound(tmp_path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("{}")
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(tmp_path / "missing.json", schema_path)
    assert exc.value.rule == "theme_file_not_found"

def test_ThemeLoader_SchemaPathMissing_RaisesConfigurationErrorSchemaFileNotFound(tmp_path):
    theme_path = tmp_path / "theme.json"
    theme_path.write_text("{}")
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, tmp_path / "missing_schema.json")
    assert exc.value.rule == "schema_file_not_found"

def test_ThemeLoader_ThemeJsonInvalid_RaisesConfigurationErrorJsonParse(tmp_path):
    theme_path = tmp_path / "theme.json"
    theme_path.write_text("{ invalid json }")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("{}")
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_parse"

def test_ThemeLoader_SchemaJsonInvalid_RaisesConfigurationErrorSchemaParse(tmp_path):
    theme_path = tmp_path / "theme.json"
    theme_path.write_text("{}")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("{ invalid json }")
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "schema_parse"

def test_ThemeLoader_ThemeFailsJsonSchemaValidation_RaisesConfigurationErrorWithPointer(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    
    del valid_theme_data["base"]
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_ThemeMissingRequiredCanvasSubtree_RaisesConfigurationError(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    
    del valid_theme_data["canvas"]
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_ExtraTopLevelKey_RaisesConfigurationError(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    
    valid_theme_data["extra_key"] = {}
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_DimZOrderEqualsHighlightZOrder_RaisesConfigurationErrorInvZ2(valid_theme_data):
    valid_theme_data["canvas"]["z_order"]["dim"] = 2.0
    valid_theme_data["canvas"]["z_order"]["highlight"] = 2.0
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z2"

def test_ThemeLoader_DimZOrderAboveHighlightZOrder_RaisesConfigurationErrorInvZ2(valid_theme_data):
    valid_theme_data["canvas"]["z_order"]["dim"] = 3.0
    valid_theme_data["canvas"]["z_order"]["highlight"] = 2.0
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z2"

def test_ThemeLoader_BasePixmapZOrderAboveDimZOrder_RaisesConfigurationErrorInvZ1(valid_theme_data):
    valid_theme_data["canvas"]["z_order"]["base_pixmap"] = 2.0
    valid_theme_data["canvas"]["z_order"]["dim"] = 1.0
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z1"

def test_ThemeLoader_HighlightScaleBelowOne_RaisesConfigurationErrorInvS1(valid_theme_data):
    valid_theme_data["canvas"]["scalar"]["highlight_scale"] = 0.5
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-S1"

def test_ThemeLoader_HighlightScaleAboveFour_RaisesConfigurationErrorInvS1(valid_theme_data):
    valid_theme_data["canvas"]["scalar"]["highlight_scale"] = 5.0
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-S1"

def test_ThemeLoader_ZoomMinScaleAboveOne_RaisesConfigurationErrorInvZ4(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["min_scale"] = 1.1
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z4"

def test_ThemeLoader_ZoomMinScaleZero_RaisesConfigurationErrorInvZ4(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["min_scale"] = 0.0
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z4"

def test_ThemeLoader_ZoomMaxScaleAtOne_RaisesConfigurationErrorInvZ5(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["max_scale"] = 1.0
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z5"

def test_ThemeLoader_ZoomMaxScaleAbove16_RaisesConfigurationErrorInvZ5(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["max_scale"] = 16.1
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z5"

def test_ThemeLoader_ZoomStepAtOne_RaisesConfigurationErrorInvZ6(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["step"] = 1.0
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z6"

def test_ThemeLoader_ZoomStepAboveTwo_RaisesConfigurationErrorInvZ6(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["step"] = 2.1
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z6"

def test_ThemeLoader_RetainedCrosshairColourKey_RaisesConfigurationError(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    valid_theme_data["canvas"]["colour"]["crosshair"] = { "rgb": "#FFFF00" }
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_RetainedChipGutterPxKey_RaisesConfigurationError(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    valid_theme_data["bom_panel"]["chip"]["gutter_px"] = 4
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_MissingChipFlowSpacingPx_RaisesConfigurationError(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    del valid_theme_data["bom_panel"]["chip"]["flow_spacing_px"]
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_MissingCanvasZoomSubtree_RaisesConfigurationError(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    del valid_theme_data["canvas"]["zoom"]
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_DimOverlayAlphaNegative_RaisesConfigurationErrorInvA1(valid_theme_data):
    valid_theme_data["canvas"]["colour"]["dim_overlay"]["alpha"] = -1
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-A1"

def test_ThemeLoader_DimOverlayAlphaAbove255_RaisesConfigurationErrorInvA1(valid_theme_data):
    valid_theme_data["canvas"]["colour"]["dim_overlay"]["alpha"] = 256
    
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-A1"

def test_ThemeLoader_ValidJsonAndSchema_ReturnsTheme(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    theme = ThemeLoader.load(theme_path, schema_path)
    assert isinstance(theme, Theme)

def test_ThemeLoader_ValidJson_ThemeSectionMapsAreFrozen(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    theme = ThemeLoader.load(theme_path, schema_path)
    with pytest.raises(Exception): # frozen dataclass mutation
        theme._base = {}


def test_Theme_CanvasColourCrosshair_RaisesKeyError():
    theme = Theme.for_testing(canvas={"colour": {"highlight_pen": {"rgb": "#FF00FF"}}})
    with pytest.raises(KeyError):
        theme.canvas_colour("crosshair")

def test_Theme_CanvasColourDimOverlay_ReturnsQColorWithDeclaredAlpha():
    theme = Theme.for_testing(canvas={"colour": {"dim_overlay": {"rgb": "#000000", "alpha": 128}}})
    color = theme.canvas_colour("dim_overlay")
    assert color.name() == "#000000"
    assert color.alpha() == 128

def test_Theme_CanvasColourHighlightPen_ReturnsQColorMagentaFullAlpha():
    theme = Theme.for_testing(canvas={"colour": {"highlight_pen": {"rgb": "#FF00FF"}}})
    color = theme.canvas_colour("highlight_pen")
    assert color.name() == "#ff00ff"
    assert color.alpha() == 255

def test_Theme_CanvasColourUnknownRole_RaisesKeyError():
    theme = Theme.for_testing()
    with pytest.raises(KeyError):
        theme.canvas_colour("unknown")

def test_Theme_CanvasPenCrosshair_RaisesKeyError():
    theme = Theme.for_testing(canvas={"colour": {"highlight_pen": {"rgb": "#FF00FF"}}, "pen_width": {"highlight_pen": 5}})
    with pytest.raises(KeyError):
        theme.canvas_pen("crosshair")

def test_Theme_CanvasPenRole_ReturnsCosmeticPenWithValueFromJson():
    theme = Theme.for_testing(canvas={
        "colour": {"highlight_pen": {"rgb": "#FF00FF"}},
        "pen_width": {"highlight_pen": 5}
    })
    pen = theme.canvas_pen("highlight_pen")
    assert pen.color().name() == "#ff00ff"
    assert pen.width() == 5
    assert pen.isCosmetic()

def test_Theme_CanvasPenUnknownRole_RaisesKeyError():
    theme = Theme.for_testing()
    with pytest.raises(KeyError):
        theme.canvas_pen("unknown")

def test_Theme_CanvasZCrosshair_RaisesKeyError():
    theme = Theme.for_testing(canvas={"z_order": {"dim": 1.5}})
    with pytest.raises(KeyError):
        theme.canvas_z("crosshair")

def test_Theme_CanvasZRole_ReturnsValueFromJson():
    theme = Theme.for_testing(canvas={"z_order": {"dim": 1.5}})
    assert theme.canvas_z("dim") == 1.5

def test_Theme_CanvasZUnknownRole_RaisesKeyError():
    theme = Theme.for_testing()
    with pytest.raises(KeyError):
        theme.canvas_z("unknown")

def test_Theme_CanvasScalarRole_ReturnsValueFromJson():
    theme = Theme.for_testing(canvas={"scalar": {"highlight_scale": 3.14}})
    assert theme.canvas_scalar("highlight_scale") == 3.14

def test_Theme_CanvasScalarUnknownRole_RaisesKeyError():
    theme = Theme.for_testing()
    with pytest.raises(KeyError):
        theme.canvas_scalar("unknown")

def test_Theme_CanvasZoomMinScale_ReturnsConfiguredValue():
    theme = Theme.for_testing(canvas={"zoom": {"min_scale": 0.5, "max_scale": 4.0, "step": 1.5}})
    assert theme.canvas_zoom_min_scale() == 0.5

def test_Theme_CanvasZoomMaxScale_ReturnsConfiguredValue():
    theme = Theme.for_testing(canvas={"zoom": {"min_scale": 0.5, "max_scale": 4.0, "step": 1.5}})
    assert theme.canvas_zoom_max_scale() == 4.0

def test_Theme_CanvasZoomStep_ReturnsConfiguredValue():
    theme = Theme.for_testing(canvas={"zoom": {"min_scale": 0.5, "max_scale": 4.0, "step": 1.5}})
    assert theme.canvas_zoom_step() == 1.5

def test_Theme_BomChipFlowSpacing_ReturnsConfiguredValue():
    theme = Theme.for_testing(bom_panel={"chip": {"flow_spacing_px": 8}})
    assert theme.bom_chip_flow_spacing() == 8

def test_Theme_Qss_RefdesChipNoLongerEmitsMarginRight(valid_theme_data):
    theme = Theme(
        _base=valid_theme_data["base"],
        _left_panel=valid_theme_data["left_panel"],
        _canvas=valid_theme_data["canvas"],
        _bom_panel=valid_theme_data["bom_panel"],
    )
    qss = theme.qss()
    assert "QLabel[class=\"refdes-chip\"]" in qss
    assert "margin-right:" not in qss.split("QLabel[class=\"refdes-chip\"] {")[1].split("}")[0]

def test_Theme_Qss_ReturnsNonEmptyString(valid_theme_data):
    theme = Theme(
        _base=valid_theme_data["base"],
        _left_panel=valid_theme_data["left_panel"],
        _canvas=valid_theme_data["canvas"],
        _bom_panel=valid_theme_data["bom_panel"],
    )
    qss = theme.qss()
    assert isinstance(qss, str)
    assert len(qss) > 0

def test_Theme_Qss_IsIdempotent(valid_theme_data):
    theme = Theme(
        _base=valid_theme_data["base"],
        _left_panel=valid_theme_data["left_panel"],
        _canvas=valid_theme_data["canvas"],
        _bom_panel=valid_theme_data["bom_panel"],
    )
    qss1 = theme.qss()
    qss2 = theme.qss()
    assert qss1 == qss2

def test_Theme_Qss_ContainsExpectedSelectorsFromEverySection(valid_theme_data):
    theme = Theme(
        _base=valid_theme_data["base"],
        _left_panel=valid_theme_data["left_panel"],
        _canvas=valid_theme_data["canvas"],
        _bom_panel=valid_theme_data["bom_panel"],
    )
    qss = theme.qss()
    assert "DropArea { background-color: #FAFAFA; }" in qss
    assert "QWidget[class~=\"checklist-row\"] { background-color:" in qss
    assert "Toast[severity=\"info\"] { background-color: #E3F2FD;" in qss
    assert "QFrame[class=\"bom-grouping\"] {" in qss
    assert "QLabel[class~=\"hint-label\"] { background-color: #FFFFFF;" in qss

def test_Theme_Frozen_AttributeMutationRaisesFrozenInstanceError(valid_theme_data):
    theme = Theme(
        _base=valid_theme_data["base"],
        _left_panel=valid_theme_data["left_panel"],
        _canvas=valid_theme_data["canvas"],
        _bom_panel=valid_theme_data["bom_panel"],
    )
    with pytest.raises(Exception):
        theme._base = {}

def test_Theme_ForTestingWithNoArgs_ReturnsUsableInstance():
    theme = Theme.for_testing()
    assert isinstance(theme, Theme)
    with pytest.raises(KeyError):
        theme.canvas_colour("dim")

def test_Theme_ForTestingPartialSection_FillsOtherSectionsWithDefaults():
    theme = Theme.for_testing(canvas={"colour": {"dim": {"rgb": "#000"}}})
    assert theme._base == {}
    assert theme._left_panel == {}
    assert theme.canvas_colour("dim").name() == "#000000"

def test_Theme_CanvasZoomRenderMultiplier_ReturnsConfiguredValue():
    theme = Theme.for_testing(canvas={"zoom": {"render_multiplier": 4.5}})
    assert theme.canvas_zoom_render_multiplier() == 4.5

def test_ThemeLoader_MissingRenderMultiplier_RaisesConfigurationError(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    del valid_theme_data["canvas"]["zoom"]["render_multiplier"]
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_RenderMultiplierBelowOne_RaisesConfigurationErrorJsonSchemaValidation(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    valid_theme_data["canvas"]["zoom"]["render_multiplier"] = 0.5
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_RenderMultiplierAboveSixteen_RaisesConfigurationErrorJsonSchemaValidation(tmp_path, valid_theme_data):
    schema_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui" / "theme.schema.json"
    valid_theme_data["canvas"]["zoom"]["render_multiplier"] = 20.0
    theme_path = tmp_path / "theme.json"
    with open(theme_path, "w") as f:
        json.dump(valid_theme_data, f)
        
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.load(theme_path, schema_path)
    assert exc.value.rule == "json_schema_validation"

def test_ThemeLoader_RenderMultiplierExceedsMaxScale_RaisesConfigurationErrorInvZ7(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["render_multiplier"] = 10.0
    valid_theme_data["canvas"]["zoom"]["max_scale"] = 8.0
    with pytest.raises(ConfigurationError) as exc:
        ThemeLoader.validate_structural_invariants(valid_theme_data)
    assert exc.value.rule == "INV-Z7"

def test_ThemeLoader_RenderMultiplierEqualsMaxScale_LoadsSuccessfully(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["render_multiplier"] = 8.0
    valid_theme_data["canvas"]["zoom"]["max_scale"] = 8.0
    ThemeLoader.validate_structural_invariants(valid_theme_data)

def test_ThemeLoader_RenderMultiplierEqualsOne_LoadsSuccessfully(valid_theme_data):
    valid_theme_data["canvas"]["zoom"]["render_multiplier"] = 1.0
    valid_theme_data["canvas"]["zoom"]["max_scale"] = 8.0
    ThemeLoader.validate_structural_invariants(valid_theme_data)
