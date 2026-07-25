"""Phase 32 facelift: dark/light presets, neon-blue accent, semantic highlights.

This module produces an *override* stylesheet appended after Theme.qss(), so
the schema-validated theme.json remains the single source of structural
styling while the facelift retunes colors. Later QSS rules win at equal
specificity, which is what makes the override approach safe.
"""

from dataclasses import dataclass

DARK = "dark"
LIGHT = "light"
PRESETS = (DARK, LIGHT)
DEFAULT_PRESET = DARK

# High-visibility selection border (replaces the old opaque yellow fill).
SELECTION_BORDER = "#FFD600"


@dataclass(frozen=True)
class Palette:
    window: str          # main window background
    surface: str         # cards / rows / panels
    surface_alt: str     # headers, alternate rows
    text: str            # primary text
    text_muted: str      # secondary text
    border: str          # subtle borders
    accent: str          # neon baby-blue trim
    accent_dim: str      # accent at low intensity (fills)
    # Semantic field colors (2.5): make key fields pop, keep legibility.
    part_number: str
    quantity: str
    process: str
    attention: str          # neon orange — ITAR text + cross-page tab cue
    overdue: str            # red — date is in the past
    due_soon: str           # amber — date within the alert window; DISTINCT from `attention` neon-orange


_PALETTES = {
    DARK: Palette(
        window="#2B2D30",
        surface="#3A3D41",
        surface_alt="#323538",
        text="#E8EAED",
        text_muted="#9AA0A6",
        border="#4A4D52",
        accent="#59D7FF",
        accent_dim="#1E3A44",
        part_number="#59D7FF",
        quantity="#7EE787",
        process="#D2A8FF",
        attention="#FFA033",
        overdue="#FF5C5C",
        due_soon="#FFC542",
    ),
    LIGHT: Palette(
        window="#F4F5F7",
        surface="#FFFFFF",
        surface_alt="#E9EBEE",
        text="#202124",
        text_muted="#5F6368",
        border="#D5D8DC",
        accent="#00A9E0",
        accent_dim="#DDF4FC",
        part_number="#0277BD",
        quantity="#2E7D32",
        process="#6A1B9A",
        attention="#E65100",
        overdue="#C62828",
        due_soon="#B26A00",
    ),
}

_active_preset = DEFAULT_PRESET


def set_active_preset(preset: str) -> None:
    global _active_preset
    _active_preset = preset if preset in _PALETTES else DEFAULT_PRESET


def active_preset() -> str:
    return _active_preset


def palette(preset: str | None = None) -> Palette:
    return _PALETTES.get(preset or _active_preset, _PALETTES[DEFAULT_PRESET])


# List View column indexes with semantic color treatment (2.5).
# 2 = B# (part number), 5 = QTY, 9 = Process.
_SEMANTIC_LIST_COLUMNS = {2: "part_number", 5: "quantity", 9: "process"}


def list_column_color(column: int):
    """QColor for a semantically highlighted List View column, else None."""
    role = _SEMANTIC_LIST_COLUMNS.get(column)
    if role is None:
        return None
    from PyQt6.QtGui import QColor
    return QColor(getattr(palette(), role))


def attention_color():
    """QColor for ITAR cells and high-visibility painted elements."""
    from PyQt6.QtGui import QColor
    return QColor(palette().attention)


def overdue_color():
    """QColor for overdue dates (red)."""
    from PyQt6.QtGui import QColor
    return QColor(palette().overdue)


def due_soon_color():
    """QColor for due soon dates (amber)."""
    from PyQt6.QtGui import QColor
    return QColor(palette().due_soon)


def compose_override_qss(preset: str, font_family: str | None = None) -> str:
    p = palette(preset)
    lines = []

    if font_family:
        lines.append(f"* {{ font-family: '{font_family}'; }}")

    lines.append(f"""
/* ---- Phase 32 facelift ({preset}) ---- */
QMainWindow, QStackedWidget, QDialog, DropArea, OpenAuditPicker, AuditView, Dashboard {{
    background-color: {p.window};
    color: {p.text};
}}
QWidget {{ color: {p.text}; }}
QLabel {{ color: {p.text}; background: transparent; }}

QPushButton {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 4px;
    padding: 4px 12px;
}}
QPushButton:hover {{
    border: 1px solid {p.accent};
    background-color: {p.accent_dim};
}}
QPushButton:pressed {{ background-color: {p.surface_alt}; }}
QPushButton:disabled {{ color: {p.text_muted}; border-color: {p.border}; }}

QLineEdit, QDateEdit, QSpinBox, QComboBox, QFontComboBox {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 4px;
    padding: 3px 6px;
    selection-background-color: {p.accent};
    selection-color: {p.window};
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{ border: 1px solid {p.accent}; }}
QComboBox QAbstractItemView {{
    background-color: {p.surface};
    color: {p.text};
    selection-background-color: {p.accent_dim};
    selection-color: {p.text};
}}

QTableView, QTableWidget {{
    background-color: {p.surface};
    alternate-background-color: {p.surface_alt};
    color: {p.text};
    gridline-color: {p.border};
    border: 1px solid {p.border};
    selection-background-color: {p.accent_dim};
    selection-color: {p.text};
}}
QHeaderView::section {{
    background-color: {p.surface_alt};
    color: {p.text};
    border: none;
    border-bottom: 2px solid {p.accent};
    padding: 4px;
    font-weight: bold;
}}
QTableView::item:selected {{ border: 1px solid {p.accent}; }}

QMenu {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
}}
QMenu::item:selected {{ background-color: {p.accent_dim}; color: {p.text}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: {p.window}; width: 12px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p.border}; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QSplitter::handle {{ background-color: {p.border}; }}
QSplitter::handle:hover {{ background-color: {p.accent}; }}

QToolTip {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.accent};
}}

/* Section headers pick up accent trim */
QLabel[class~="section-header"], QLabel[class~="bom-sticky-header"] {{
    background-color: {p.surface_alt};
    color: {p.text};
    border-left: 3px solid {p.accent};
}}

/* Checklist rows (THT/SMT/Notes): readable surface + border selection.
   The old opaque yellow fill obscured item numbers; a yellow border keeps
   every character legible (2.5). */
QFrame[class~="checklist-row"] {{
    background-color: {p.surface};
    border: 1px solid {p.border};
}}
QFrame[class~="checklist-row"][selected="true"] {{
    background-color: {p.surface};
    border: 2px solid {SELECTION_BORDER};
}}
QFrame[class="bom-grouping"] {{
    background-color: {p.surface};
    border: 1px solid {p.border};
}}
QFrame[class="bom-grouping"][selected="true"] {{
    background-color: {p.surface};
    border: 2px solid {SELECTION_BORDER};
}}

/* Semantic highlighting in the Audit View (2.5):
   part number pops in accent, description stays muted. */
QFrame[class="cell-mpn"] {{ background-color: {p.surface_alt}; }}
QFrame[class="cell-mpn"] QLabel {{ color: {p.part_number}; font-weight: bold; }}
QFrame[class="cell-refdes"] {{ background-color: {p.surface_alt}; }}
QFrame[class="cell-refdes"] QLabel {{ color: {p.text}; }}
QFrame[class="cell-description"] {{ background-color: {p.surface_alt}; }}
QFrame[class="cell-description"] QLabel {{ color: {p.text_muted}; }}
QFrame[class="cell-find"] {{ background-color: {p.surface_alt}; }}
QFrame[class="cell-find"] QLabel {{ color: {p.quantity}; font-weight: bold; }}

QLabel[class="refdes-chip"] {{
    background-color: {p.accent_dim};
    color: {p.text};
    border: 1px solid {p.accent};
}}
QLabel[class="refdes-chip"]:hover {{ background-color: {p.accent}; color: {p.window}; }}

/* Audit-View header semantic colors (Patch 02, 2.4) — value labels only */
QLabel[class~="hdr-part-number"] {{ color: {p.part_number}; font-weight: bold; }}
QLabel[class~="hdr-qty"]         {{ color: {p.quantity};   font-weight: bold; }}
QLabel[class~="hdr-process"]     {{ color: {p.process};    font-weight: bold; }}

/* ITAR indicator (Patch 02, 2.4) — plain orange text, both views */
QLabel[class~="itar-badge"]      {{ color: {p.attention};  font-weight: bold; }}

/* Image tabs (Patch 02, 2.2) — constant 2px border avoids layout shift.
   :checked is listed LAST so the active (yellow) state wins any equal-specificity
   tie with [indicator] during the transient handled in §3.5. */
PageSwitcher QPushButton                     {{ border: 2px solid {p.border}; }}
PageSwitcher QPushButton[indicator="true"]   {{ border: 2px solid {p.attention}; }}
PageSwitcher QPushButton:checked             {{ border: 2px solid {SELECTION_BORDER}; }}
""")
    return "\n".join(lines)
