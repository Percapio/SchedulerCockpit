"""Theme infrastructure."""

import json
import pathlib
import jsonschema
from dataclasses import dataclass
from typing import Any, Mapping

from PyQt6.QtGui import QColor, QPen, QBrush
from PyQt6.QtCore import Qt


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
class Theme:
    """
    Intent:   Immutable view over the loaded and validated theme.
    """

    _base:       Mapping[str, Any]
    _left_panel: Mapping[str, Any]
    _canvas:     Mapping[str, Any]
    _bom_panel:  Mapping[str, Any]

    def qss(self) -> str:
        """
        Intent:   Compose and return the full application stylesheet.
        """
        base = self._base
        lp = self._left_panel
        bp = self._bom_panel
        cv = self._canvas
        
        # Base window
        qss_lines = []
        
        # DropArea
        # Pre-Phase-12 had DropArea background-color #fafafa
        qss_lines.append(f"DropArea {{ background-color: {base['window']['rgb']}; }}")
        qss_lines.append("DropArea[state=\"resting\"] { border: 3px dashed #cccccc; border-radius: 12px; }")
        qss_lines.append("DropArea[state=\"active\"] { border: 4px dashed #4caf50; border-radius: 12px; background-color: #e8f5e9; }")
        qss_lines.append("DropArea[state=\"disabled\"] { border: none; background-color: #f0f0f0; }")
        
        qss_lines.append(f"QLabel#DropAreaMainLabel {{ font-size: 24px; font-weight: bold; color: {lp['section_header']['text_rgb']}; }}")
        qss_lines.append("DropArea[state=\"active\"] QLabel#DropAreaMainLabel { color: #2e7d32; }")
        qss_lines.append("QLabel#DropAreaSubLabel { font-size: 16px; color: #888888; }")
        qss_lines.append("DropArea[state=\"active\"] QLabel#DropAreaSubLabel { color: #4caf50; }")
        
        # ProgressView styles
        qss_lines.append("QLabel#ProgressText { font-size: 16px; }")
        qss_lines.append("QLabel#ProgressIcon { font-size: 18px; }")
        qss_lines.append("QLabel#ProgressText[status=\"completed\"] { font-weight: bold; }")
        qss_lines.append("QLabel#ProgressIcon[status=\"completed\"] { color: green; }")
        qss_lines.append("QLabel#ProgressText[status=\"pending\"] { font-weight: normal; color: gray; }")
        qss_lines.append("QLabel#ProgressIcon[status=\"pending\"] { color: gray; }")
        
        # ErrorDialog styles
        qss_lines.append("QLabel#ErrorSummary { font-size: 14px; font-weight: bold; }")
        qss_lines.append("QLabel#ErrorFooter { font-family: monospace; font-size: 11px; color: #666666; }")
        
        # ChecklistRow
        qss_lines.append(f"QWidget[class~=\"checklist-row\"] {{ background-color: {lp['row']['fill_rgb']}; border-radius: {lp['row']['corner_radius_px']}px; padding: {lp['row']['vertical_padding_px']}px {lp['row']['horizontal_padding_px']}px; }}")
        qss_lines.append(f"QWidget[class~=\"checklist-row\"][selected=\"true\"] {{ background-color: {lp['row']['fill_selected_rgb']}; }}")
        qss_lines.append(f"QWidget[class~=\"checklist-row\"][selected=\"true\"] > QLabel {{ color: {lp['row']['text_selected_rgb']}; }}")
        qss_lines.append("QPushButton[indicator=\"true\"] { }")
        
        # Section Header
        qss_lines.append(f"QLabel[class~=\"section-header\"] {{ background-color: {lp['section_header']['fill_rgb']}; color: {lp['section_header']['text_rgb']}; padding: {lp['section_header']['padding_px']}px; font-weight: bold; font-size: 16px; }}")
        
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
        qss_lines.append(f"ShipDateField QLabel {{ color: {lp['ship_date_field']['text_rgb']}; font-size: 11px; font-weight: bold; }}")
        qss_lines.append("ShipDateField QDateEdit { font-size: 14px; }")
        
        # AuditBomRow
        qss_lines.append(f"AuditBomRow {{ background-color: transparent; border-bottom: {bp['grouping']['border_width_px']}px solid {bp['grouping']['border_rgb']}; }}")
        qss_lines.append(f"AuditBomRow[selected=\"true\"] {{ background-color: {bp['cell']['mpn']['fill_rgb']}; }}")
        
        qss_lines.append(f"QLabel[class=\"mpn-cell\"] {{ font-weight: bold; min-width: 80px; color: {bp['cell']['mpn']['text_rgb']}; }}")
        qss_lines.append(f"AuditBomRow QLabel[class=\"desc-cell\"] {{ color: {bp['cell']['description']['text_rgb']}; font-size: {bp['cell']['description']['font_size_px']}px; }}")
        
        qss_lines.append(f"RefDesChip {{ background-color: {bp['chip']['fill_rgb']}; color: {bp['chip']['text_rgb']}; border-radius: {bp['chip']['corner_radius_px']}px; padding: {bp['chip']['padding_px']}px {bp['chip']['padding_px']*2}px; margin: {bp['chip']['gutter_px']}px; }}")
        qss_lines.append("RefDesChip[selected=\"true\"] { background-color: #007ACC; color: white; font-weight: bold; }")
        
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

    @classmethod
    def for_testing(cls, base=None, left_panel=None, canvas=None, bom_panel=None) -> 'Theme':
        """
        Intent:   Construct a Theme for unit tests. Bypasses file I/O and
                  schema validation. Unprovided sections default to empty dicts.
        Raises:   Never. Test fixtures may be structurally under-constrained.
        """
        return cls(
            _base=base or {},
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
            raise ConfigurationError(
                pointer="/", 
                rule="json_parse", 
                detail=f"{theme_path}: {str(e)}"
            )
            
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigurationError(
                pointer="/", 
                rule="schema_parse", 
                detail=f"{schema_path}: {str(e)}"
            )
            
        try:
            jsonschema.validate(instance=theme_data, schema=schema_data)
        except jsonschema.ValidationError as e:
            path = "/" + "/".join(str(p) for p in e.path)
            raise ConfigurationError(
                pointer=path, 
                rule="json_schema_validation", 
                detail=e.message
            )
            
        cls.validate_structural_invariants(theme_data)
        
        return Theme(
            _base=theme_data["base"],
            _left_panel=theme_data["left_panel"],
            _canvas=theme_data["canvas"],
            _bom_panel=theme_data["bom_panel"]
        )

    @classmethod
    def validate_structural_invariants(cls, data: Mapping[str, Any]) -> None:
        cv = data["canvas"]
        z = cv["z_order"]
        if not (z["base_pixmap"] < z["dim"]):
            raise ConfigurationError("/canvas/z_order", "INV-Z1", "base_pixmap must be < dim")
        if not (z["dim"] < z["highlight"]):
            raise ConfigurationError("/canvas/z_order", "INV-Z2", "dim must be < highlight")
        if not (z["highlight"] < z["crosshair"]):
            raise ConfigurationError("/canvas/z_order", "INV-Z3", "highlight must be < crosshair")
            
        s = cv["scalar"]["highlight_scale"]
        if not (1.0 <= s <= 4.0):
            raise ConfigurationError("/canvas/scalar/highlight_scale", "INV-S1", "must be between 1.0 and 4.0")
            
        for role, entry in cv["colour"].items():
            if "alpha" in entry:
                a = entry["alpha"]
                if not (0 <= a <= 255):
                    raise ConfigurationError(f"/canvas/colour/{role}/alpha", "INV-A1", "must be between 0 and 255")
