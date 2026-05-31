"""Theme infrastructure."""

import json
import pathlib
import jsonschema
from dataclasses import dataclass
from typing import Any, Mapping

from PyQt6.QtGui import QColor, QPen, QBrush
from PyQt6.QtCore import Qt
import logging
logger = logging.getLogger(__name__)



class ConfigurationError(Exception):
    """
    Intent:   Raised when the theme file cannot be loaded, parsed, or validated
              against theme.schema.json, or when a structural invariant
              is violated.
    """

    pointer:  str    # JSON Pointer (RFC 6901) into the offending path, or '/'
    rule:     str    # Human-readable rule name that was violated
    detail:   str    # Human-readable diagnostic (e.g., file path, specific error message)

    def __init__(self, pointer: str, rule: str, detail: str = "") -> None:
        self.pointer = pointer
        self.rule = rule
        self.detail = detail

    def __str__(self) -> str:
        """Returns 'ConfigurationError at <pointer>: <rule> — <detail>'."""
        return f"ConfigurationError at {self.pointer}: {self.rule} \u2014 {self.detail}"


@dataclass(frozen=True)
class FontScaleBounds:
    default_pt: int
    min_pt:     int
    max_pt:     int
    step_pt:    int

@dataclass(frozen=True)
class Theme:
    """
    Intent:   Immutable view over the loaded and validated theme.
    """

    _application: Mapping[str, Any]
    _base:       Mapping[str, Any]
    _left_panel: Mapping[str, Any]
    _canvas:     Mapping[str, Any]
    _bom_panel:  Mapping[str, Any]

    def font_scale_bounds(self) -> FontScaleBounds:
        block = self._application["font_scale"]
        return FontScaleBounds(
            default_pt=block["default_pt"],
            min_pt=block["min_pt"],
            max_pt=block["max_pt"],
            step_pt=block["step_pt"]
        )

    def qss(self, font_scale_pt: int | None = None) -> str:
        """
        Intent:   Compose and return the full application stylesheet.
        """
        base = self._base
        lp = self._left_panel
        bp = self._bom_panel
        cv = self._canvas
        
        bounds = self.font_scale_bounds()
        pt = font_scale_pt if font_scale_pt is not None else bounds.default_pt
        
        def scale(base_px: int) -> int:
            return round(base_px * (pt / bounds.default_pt))

        # Base window
        qss_lines = []
        qss_lines.append(f"* {{ font-family: '{base['font']['family']}'; font-size: {scale(base['font']['size_px'])}px; }}")
        
        # DropArea
        qss_lines.append(f"DropArea {{ background-color: {base['window']['rgb']}; }}")
        qss_lines.append("DropArea[state=\"resting\"] { border: 3px dashed #cccccc; border-radius: 12px; }")
        qss_lines.append("DropArea[state=\"active\"] { border: 4px dashed #4caf50; border-radius: 12px; background-color: #e8f5e9; }")
        qss_lines.append("DropArea[state=\"disabled\"] { border: none; background-color: #f0f0f0; }")
        
        qss_lines.append(f"QLabel#DropAreaMainLabel {{ font-size: {scale(24)}px; font-weight: bold; color: {lp['section_header']['text_rgb']}; }}")
        qss_lines.append("DropArea[state=\"active\"] QLabel#DropAreaMainLabel { color: #2e7d32; }")
        qss_lines.append(f"QLabel#DropAreaSubLabel {{ font-size: {scale(16)}px; color: #888888; }}")
        qss_lines.append("DropArea[state=\"active\"] QLabel#DropAreaSubLabel { color: #4caf50; }")
        
        # ProgressView styles
        qss_lines.append(f"QLabel#ProgressText {{ font-size: {scale(16)}px; }}")
        qss_lines.append(f"QLabel#ProgressIcon {{ font-size: {scale(18)}px; }}")
        qss_lines.append("QLabel#ProgressText[status=\"completed\"] { font-weight: bold; }")
        qss_lines.append("QLabel#ProgressIcon[status=\"completed\"] { color: green; }")
        qss_lines.append("QLabel#ProgressText[status=\"pending\"] { font-weight: normal; color: gray; }")
        qss_lines.append("QLabel#ProgressIcon[status=\"pending\"] { color: gray; }")
        
        # ErrorDialog styles
        qss_lines.append(f"QLabel#ErrorSummary {{ font-size: {scale(14)}px; font-weight: bold; }}")
        qss_lines.append(f"QLabel#ErrorFooter {{ font-family: monospace; font-size: {scale(11)}px; color: #666666; }}")
        
        # ChecklistRow
        qss_lines.append(f"QFrame[class~=\"checklist-row\"] {{ background-color: {lp['row']['fill_rgb']}; border-radius: {lp['row']['corner_radius_px']}px; padding: {lp['row']['vertical_padding_px']}px {lp['row']['horizontal_padding_px']}px; border: {bp['grouping']['border_width_px']}px solid {bp['grouping']['border_rgb']}; }}")
        qss_lines.append(f"QFrame[class~=\"checklist-row\"][selected=\"true\"] {{ background-color: {lp['row']['fill_selected_rgb']}; }}")
        qss_lines.append("QPushButton[indicator=\"true\"] { }")
        
        # Section Header
        qss_lines.append(f"QLabel[class~=\"section-header\"] {{ background-color: {lp['section_header']['fill_rgb']}; color: {lp['section_header']['text_rgb']}; padding: {lp['section_header']['padding_px']}px; font-weight: bold; font-size: {scale(16)}px; }}")
        
        # Toast
        qss_lines.append(f"Toast[severity=\"info\"] {{ background-color: {base['toast']['info']['background_rgb']}; border: 1px solid {base['toast']['info']['border_rgb']}; border-radius: 8px; }}")
        qss_lines.append(f"Toast[severity=\"info\"] QLabel#ToastTitle {{ color: {base['toast']['info']['text_rgb']}; font-weight: bold; }}")
        qss_lines.append(f"Toast[severity=\"info\"] QLabel#ToastSubtitle {{ color: {base['toast']['info']['text_rgb']}; }}")
        
        qss_lines.append(f"Toast[severity=\"warn\"] {{ background-color: {base['toast']['warn']['background_rgb']}; border: 1px solid {base['toast']['warn']['border_rgb']}; border-radius: 8px; }}")
        qss_lines.append(f"Toast[severity=\"warn\"] QLabel#ToastTitle {{ color: {base['toast']['warn']['text_rgb']}; font-weight: bold; }}")
        
        qss_lines.append(f"Toast[severity=\"error\"] {{ background-color: {base['toast']['error']['background_rgb']}; border: 1px solid {base['toast']['error']['border_rgb']}; border-radius: 8px; }}")
        qss_lines.append(f"Toast[severity=\"error\"] QLabel#ToastTitle {{ color: {base['toast']['error']['text_rgb']}; font-weight: bold; }}")
        qss_lines.append(f"Toast[severity=\"error\"] QLabel#ToastSubtitle {{ color: {base['toast']['error']['text_rgb']}; }}")
        
        # ShipDateField
        qss_lines.append(f"ShipDateField QLabel {{ color: {lp['ship_date_field']['text_rgb']}; font-size: {scale(11)}px; font-weight: bold; }}")
        qss_lines.append(f"ShipDateField QDateEdit {{ font-size: {scale(14)}px; }}")
        
        # BOM Panel
        qss_lines.append(self._compose_bom_grouping(bp['grouping']))
        qss_lines.append(self._compose_bom_cells(bp['cell'], pt, bounds.default_pt))
        qss_lines.append(self._compose_bom_chip(bp['chip']))
        
        qss_lines.append("QLabel[class~=\"empty-bom-label\"] { color: #888888; padding: 20px; }")
        
        # LayoutCanvas hint label
        qss_lines.append(f"QLabel[class~=\"hint-label\"] {{ background-color: {cv['colour']['hint_label_background']['rgb']}; color: {cv['colour']['hint_label_text']['rgb']}; padding: {cv['hint_label']['padding_px']}px; border: {cv['hint_label']['border_width_px']}px solid {cv['colour']['hint_label_border']['rgb']}; }}")
        
        return "\n".join(qss_lines)

    def canvas_colour(self, role: str) -> QColor:
        if role not in self._canvas['colour']:
            raise KeyError(role)
        entry = self._canvas['colour'][role]
        rgb = entry['rgb']
        alpha = entry.get('alpha', 255)
        color = QColor(rgb)
        color.setAlpha(alpha)
        return color

    def canvas_pen(self, role: str) -> QPen:
        if role not in self._canvas['pen_width'] or role not in self._canvas['colour']:
            raise KeyError(role)
        width = self._canvas['pen_width'][role]
        color = self.canvas_colour(role)
        pen = QPen(color)
        pen.setWidth(width)
        pen.setCosmetic(True)
        return pen

    def canvas_brush(self, role: str) -> QBrush:
        if role not in self._canvas['colour']:
            raise KeyError(role)
        return QBrush(self.canvas_colour(role))

    def canvas_z(self, role: str) -> float:
        if role not in self._canvas['z_order']:
            raise KeyError(role)
        return float(self._canvas['z_order'][role])

    def canvas_scalar(self, role: str) -> float:
        if role not in self._canvas['scalar']:
            raise KeyError(role)
        return float(self._canvas['scalar'][role])

    def canvas_zoom_min_scale(self) -> float:
        return float(self._canvas['zoom']['min_scale'])

    def canvas_zoom_max_scale(self) -> float:
        return float(self._canvas['zoom']['max_scale'])

    def canvas_zoom_step(self) -> float:
        return float(self._canvas['zoom']['step'])

    def canvas_zoom_render_multiplier(self) -> float:
        return float(self._canvas['zoom']['render_multiplier'])

    def bom_chip_flow_spacing(self) -> int:
        return int(self._bom_panel['chip']['flow_spacing_px'])

    def left_panel_min_width(self) -> int:
        return int(self._left_panel['layout']['min_width_px'])

    def bom_panel_min_width_percent(self) -> float:
        return float(self._bom_panel['layout']['min_width_percent'])

    def bom_panel_min_width_absolute(self) -> int:
        return int(self._bom_panel['layout']['min_width_absolute_px'])

    def _compose_bom_grouping(self, grouping_tokens: Mapping[str, Any]) -> str:
        lines = [
            "QFrame[class=\"bom-grouping\"] {",
            f"    background-color: {grouping_tokens['fill_rgb']};",
            f"    border: {grouping_tokens['border_width_px']}px solid {grouping_tokens['border_rgb']};",
            f"    border-radius: {grouping_tokens['corner_radius_px']}px;",
            f"    padding: {grouping_tokens['inner_padding_px']}px;",
            f"    margin-bottom: {grouping_tokens['gutter_px']}px;",
            "}",
            "QFrame[class=\"bom-grouping\"][selected=\"true\"] {",
            f"    background-color: {grouping_tokens['fill_selected_rgb']};",
            "}"
        ]
        return "\n".join(lines)

    def _compose_bom_cells(self, cell_tokens: Mapping[str, Mapping[str, Any]], pt: int, default_pt: int) -> str:
        lines = []
        for role, tokens in cell_tokens.items():
            lines.extend([
                f"QFrame[class=\"cell-{role}\"] {{",
                f"    background-color: {tokens['fill_rgb']};",
                f"    border-radius: {tokens['corner_radius_px']}px;",
                f"    padding: {tokens['padding_px']}px;",
                "    border: none;",
                "}",
                f"QFrame[class=\"cell-{role}\"] QLabel {{",
                f"    color: {tokens['text_rgb']};"
            ])
            if "font_size_px" in tokens:
                sz = round(tokens['font_size_px'] * (pt / default_pt))
                lines.append(f"    font-size: {sz}px;")
            lines.extend([
                "}"
            ])
        return "\n".join(lines)

    def _compose_bom_chip(self, chip_tokens: Mapping[str, Any]) -> str:
        lines = [
            "QLabel[class=\"refdes-chip\"] {",
            f"    background-color: {chip_tokens['fill_rgb']};",
            f"    color: {chip_tokens['text_rgb']};",
            f"    border-radius: {chip_tokens['corner_radius_px']}px;",
            f"    padding: {chip_tokens['vertical_padding_px']}px {chip_tokens['horizontal_padding_px']}px;",
            "}",
            "QLabel[class=\"refdes-chip\"]:hover {",
            f"    background-color: {chip_tokens['fill_hover_rgb']};",
            "}"
        ]
        return "\n".join(lines)

    @classmethod
    def for_testing(cls, application=None, base=None, left_panel=None, canvas=None, bom_panel=None) -> 'Theme':
        """
        Intent:   Construct a Theme for unit tests. Bypasses file I/O and
                  schema validation. Unprovided sections default to empty dicts.
        Raises:   Never. Test fixtures may be structurally under-constrained.
        """
        b = base or {}
        if "font" not in b:
            b["font"] = {"family": "Segoe UI", "size_px": 13}
            
        return cls(
            _application=application or {},
            _base=b,
            _left_panel=left_panel or {},
            _canvas=canvas or {},
            _bom_panel=bom_panel or {}
        )


class ThemeLoader:
    @classmethod
    def load(cls, theme_path: pathlib.Path, schema_path: pathlib.Path) -> Theme:
        if not theme_path.exists():
            raise ConfigurationError(
                pointer="/", 
                rule="theme_file_not_found", 
                detail=str(theme_path)
            )
            
        if not schema_path.exists():
            raise ConfigurationError(
                pointer="/", 
                rule="schema_file_not_found", 
                detail=str(schema_path)
            )
            
        try:
            with open(theme_path, "r", encoding="utf-8") as f:
                theme_data = json.load(f)
        except json.JSONDecodeError as e:
            logger.exception('Exception caught in theme')
            raise ConfigurationError(
                pointer="/", 
                rule="json_parse", 
                detail=f"{theme_path}: {str(e)}"
            )
            
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema_data = json.load(f)
        except json.JSONDecodeError as e:
            logger.exception('Exception caught in theme')
            raise ConfigurationError(
                pointer="/", 
                rule="schema_parse", 
                detail=f"{schema_path}: {str(e)}"
            )
            
        try:
            jsonschema.validate(instance=theme_data, schema=schema_data)
        except jsonschema.ValidationError as e:
            logger.exception('Exception caught in theme')
            path = "/" + "/".join(str(p) for p in e.path)
            raise ConfigurationError(
                pointer=path, 
                rule="json_schema_validation", 
                detail=e.message
            )
            
        cls.validate_structural_invariants(theme_data)
        
        return Theme(
            _application=theme_data["application"],
            _base=theme_data["base"],
            _left_panel=theme_data["left_panel"],
            _canvas=theme_data["canvas"],
            _bom_panel=theme_data["bom_panel"]
        )

    @classmethod
    def validate_structural_invariants(cls, data: Mapping[str, Any]) -> None:
        fs = data["application"]["font_scale"]
        if not (fs["min_pt"] <= fs["default_pt"] <= fs["max_pt"]):
            raise ConfigurationError("/application/font_scale/default_pt", "INV-FS1", "must be within min_pt and max_pt")
        
        cv = data["canvas"]
        z = cv["z_order"]
        if not (z["base_pixmap"] < z["dim"]):
            raise ConfigurationError("/canvas/z_order", "INV-Z1", "base_pixmap must be < dim")
        if not (z["dim"] < z["highlight"]):
            raise ConfigurationError("/canvas/z_order", "INV-Z2", "dim must be < highlight")
            
        s = cv["scalar"]["highlight_scale"]
        if not (1.0 <= s <= 4.0):
            raise ConfigurationError("/canvas/scalar/highlight_scale", "INV-S1", "must be between 1.0 and 4.0")
            
        zm = cv["zoom"]
        if not (0 < zm["min_scale"] <= 1.0):
            raise ConfigurationError("/canvas/zoom/min_scale", "INV-Z4", "must be > 0 and <= 1.0")
        if not (1.0 < zm["max_scale"] <= 16.0):
            raise ConfigurationError("/canvas/zoom/max_scale", "INV-Z5", "must be > 1.0 and <= 16.0")
        if not (1.0 < zm["step"] <= 2.0):
            raise ConfigurationError("/canvas/zoom/step", "INV-Z6", "must be > 1.0 and <= 2.0")
            
        rm = zm["render_multiplier"]
        ms = zm["max_scale"]
        if rm < 1.0 or rm > ms:
            raise ConfigurationError(
                pointer="/canvas/zoom/render_multiplier",
                rule="INV-Z7",
                detail=f"render_multiplier={rm} must be in [1.0, {ms}]"
            )
            
        for role, entry in cv["colour"].items():
            if "alpha" in entry:
                a = entry["alpha"]
                if not (0 <= a <= 255):
                    raise ConfigurationError(f"/canvas/colour/{role}/alpha", "INV-A1", "must be between 0 and 255")
