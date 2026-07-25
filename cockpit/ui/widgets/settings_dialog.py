"""Settings dialog (Phase 32, 2.4 + 3.2)."""

import datetime
import logging
import pathlib
from dataclasses import dataclass
from functools import partial

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QComboBox, QFontComboBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QPushButton, QLabel, QFileDialog, QMessageBox, QWidget
)
from PyQt6.QtGui import QFont

from cockpit.ui import facelift
from cockpit.ui.ui_prefs import StyleController, export_diagnostics, RuntimeCalcSettingsController
from cockpit.ui.font_scale_controller import FontScaleController

logger = logging.getLogger(__name__)

_PRESET_LABELS = {facelift.DARK: "Dark", facelift.LIGHT: "Light"}

@dataclass(frozen=True)
class FieldSpec:
    label: str
    min: float
    max: float
    step: float
    decimals: int
    suffix: str

_RUNTIME_FIELD_SPECS: tuple[tuple[str, FieldSpec], ...] = (
    ("smt_placement_time_min",       FieldSpec("SMT placement time",             0.001,  1.0,    0.001,  3, " min")),
    ("tht_placement_time_min",       FieldSpec("THT placement time",             0.01,   5.0,    0.01,   2, " min")),
    ("aoi_inspection_time_min",      FieldSpec("AOI inspection time",            0.0001, 0.01,   0.0001, 4, " min")),
    ("class_3_multiplier_aoi",       FieldSpec("Class 3 multiplier (AOI)",       1.0,    3.0,    0.05,   2, "×")),
    ("class_3_multiplier_tht",       FieldSpec("Class 3 multiplier (THT)",       1.0,    3.0,    0.05,   2, "×")),
    ("clean_process_multiplier_tht", FieldSpec("Clean-process multiplier (THT)", 1.0,    3.0,    0.05,   2, "×")),
    ("clean_process_multiplier_ops", FieldSpec("Clean-process multiplier (OPS)", 1.0,    3.0,    0.05,   2, "×")),
    ("shipping_flat_hours",          FieldSpec("Shipping setup (flat)",          0.0,    40.0,   0.5,    1, " hr")),
    ("shipping_boards_per_hour",     FieldSpec("Shipping rate",                  1.0,    1000.0, 10.0,   0, "/hr")),
)




class SettingsDialog(QDialog):
    """Display preferences (theme preset, font family, font size) + diagnostics export.

    Changes apply live and persist via the controllers' QSettings backing.
    """

    def __init__(
        self,
        style_controller: StyleController,
        font_scale_controller: FontScaleController,
        runtime_settings_controller: RuntimeCalcSettingsController | None,
        config,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self._style = style_controller
        self._font_scale = font_scale_controller
        self._config = config

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.theme_combo = QComboBox()
        for preset in facelift.PRESETS:
            self.theme_combo.addItem(_PRESET_LABELS[preset], preset)
        idx = self.theme_combo.findData(self._style.preset())
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        form.addRow("Theme:", self.theme_combo)

        self.font_combo = QFontComboBox()
        current_family = self._style.font_family()
        if current_family:
            self.font_combo.setCurrentFont(QFont(current_family))
        self.font_combo.currentFontChanged.connect(self._on_font_changed)
        form.addRow("Font:", self.font_combo)

        bounds = self._font_scale._bounds
        self.size_spin = QSpinBox()
        self.size_spin.setRange(bounds.min_pt, bounds.max_pt)
        self.size_spin.setSingleStep(bounds.step_pt)
        self.size_spin.setValue(self._font_scale.current_pt())
        self.size_spin.setSuffix(" pt")
        self.size_spin.valueChanged.connect(self._on_size_changed)
        form.addRow("Font size:", self.size_spin)

        layout.addLayout(form)
        layout.addSpacing(12)

        if runtime_settings_controller is not None:
            calc_group = self._build_calculation_settings(runtime_settings_controller)
            layout.addWidget(calc_group)
            layout.addSpacing(12)

        diag_label = QLabel("Trouble? Package the logs for the development team:")
        diag_label.setWordWrap(True)
        layout.addWidget(diag_label)

        export_btn = QPushButton("Export Diagnostics/Logs...")
        export_btn.clicked.connect(self._on_export_diagnostics)
        layout.addWidget(export_btn)

        layout.addStretch()

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    # --- live-apply handlers ---

    def _on_theme_changed(self, _index: int) -> None:
        self._style.set_preset(self.theme_combo.currentData())

    def _on_font_changed(self, font: QFont) -> None:
        self._style.set_font_family(font.family())

    def _on_size_changed(self, pt: int) -> None:
        delta_pt = pt - self._font_scale.current_pt()
        step = self._font_scale._bounds.step_pt
        if step and delta_pt % step == 0 and delta_pt != 0:
            self._font_scale.request_delta(delta_pt // step)

    def _on_export_diagnostics(self) -> None:
        stamp = datetime.date.today().isoformat()
        suggested = str(pathlib.Path.home() / f"cockpit-diagnostics-{stamp}.zip")
        target, _ = QFileDialog.getSaveFileName(
            self, "Export Diagnostics", suggested, "Zip archives (*.zip)"
        )
        if not target:
            return
        try:
            archived = export_diagnostics(self._config, pathlib.Path(target))
        except Exception as e:
            logger.exception("Diagnostics export failed")
            QMessageBox.critical(self, "Export failed", f"Could not write the archive:\n{e}")
            return
        QMessageBox.information(
            self, "Diagnostics exported",
            f"Packaged {len(archived)} file(s) into:\n{target}"
        )

    def _build_calculation_settings(self, controller: RuntimeCalcSettingsController) -> QGroupBox:
        current_constants = controller.constants()
        group = QGroupBox("Calculation Settings")
        form = QFormLayout(group)
        for field_name, spec in _RUNTIME_FIELD_SPECS:
            spin = QDoubleSpinBox()
            spin.setRange(spec.min, spec.max)
            spin.setSingleStep(spec.step)
            spin.setDecimals(spec.decimals)
            spin.setSuffix(spec.suffix)
            spin.valueChanged.connect(partial(controller.set_value, field_name))
            spin.blockSignals(True)
            spin.setValue(getattr(current_constants, field_name))
            spin.blockSignals(False)
            if field_name == "clean_process_multiplier_ops":
                spin.setToolTip("Applies once OPS is computed (a later phase)")
            form.addRow(spec.label + ":", spin)
        return group
