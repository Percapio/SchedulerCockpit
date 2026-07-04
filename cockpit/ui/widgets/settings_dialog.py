"""Settings dialog (Phase 32, 2.4 + 3.2)."""

import datetime
import logging
import pathlib

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QComboBox, QFontComboBox,
    QSpinBox, QPushButton, QLabel, QFileDialog, QMessageBox, QWidget
)
from PyQt6.QtGui import QFont

from cockpit.ui import facelift
from cockpit.ui.ui_prefs import StyleController, export_diagnostics
from cockpit.ui.font_scale_controller import FontScaleController

logger = logging.getLogger(__name__)

_PRESET_LABELS = {facelift.DARK: "Dark", facelift.LIGHT: "Light"}


class SettingsDialog(QDialog):
    """Display preferences (theme preset, font family, font size) + diagnostics export.

    Changes apply live and persist via the controllers' QSettings backing.
    """

    def __init__(
        self,
        style_controller: StyleController,
        font_scale_controller: FontScaleController,
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
