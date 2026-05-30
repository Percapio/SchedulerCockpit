"""Checklist row widget."""

from PyQt6.QtCore import pyqtSignal, Qt, QEvent, QObject
from PyQt6.QtGui import QMouseEvent, QCursor
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QCheckBox, QLabel, QLineEdit

from cockpit.services.views import ChecklistRowView, ChecklistRowKey, ChecklistRowKind
from cockpit.ui.widgets.refdes_chip import RefDesChip
from cockpit.ui.widgets.flow_layout import FlowLayout
import logging
logger = logging.getLogger(__name__)


class ChecklistRow(QWidget):
    toggle_requested = pyqtSignal(object, bool)
    body_clicked = pyqtSignal(object)
    mpn_clicked = pyqtSignal(object)
    refdes_chip_clicked = pyqtSignal(str)

    def __init__(self, row: ChecklistRowView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row = row
        self._ignore_signals = False
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setProperty("class", "checklist-row")
        
        self.checkbox = QCheckBox()
        self.checkbox.toggled.connect(self._on_toggled)
        layout.addWidget(self.checkbox)
        
        self.primary_lbl = QLabel()
        self.primary_lbl.setProperty("class", "bold")
        
        self.secondary_lbl = QLabel()
        self.secondary_lbl.setWordWrap(True)
        
        if row.key.kind == ChecklistRowKind.THT:
            self.find_number_lbl = QLabel()
            self.find_number_lbl.setProperty("class", "find-number-badge")
            self.find_number_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.find_number_lbl, stretch=10)

            layout.addWidget(self.primary_lbl, stretch=25)

            self.chip_strip = QWidget()
            self.chip_layout = FlowLayout(self.chip_strip, margin=0, h_spacing=4, v_spacing=4)
            layout.addWidget(self.chip_strip, stretch=30)
            
            layout.addWidget(self.secondary_lbl, stretch=35)
            self.chips: dict[str, RefDesChip] = {}
        else:
            layout.addWidget(self.primary_lbl)
            layout.addWidget(self.secondary_lbl, stretch=1)
        
        self._apply_view(row)

    def _apply_view(self, view: ChecklistRowView) -> None:
        self._ignore_signals = True
        try:
            self.checkbox.setChecked(view.is_verified)
            self.primary_lbl.setText(view.primary_label)
            self.secondary_lbl.setText(view.secondary_label or "")
            
            if view.key.kind == ChecklistRowKind.THT:
                self.primary_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                self.primary_lbl.setProperty("role", "mpn")
                self.primary_lbl.installEventFilter(self)
                
                if view.find_number is not None:
                    self.find_number_lbl.setText(str(view.find_number))
                else:
                    self.find_number_lbl.setText("")
                    
                while self.chip_layout.count():
                    item = self.chip_layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
                self.chips.clear()
                
                for rd in view.ref_des_list:
                    chip = RefDesChip(rd)
                    chip.clicked.connect(self.refdes_chip_clicked.emit)
                    self.chip_layout.addWidget(chip)
                    self.chips[rd] = chip
        finally:
            self._ignore_signals = False
            
        self.checkbox.setEnabled(True)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (obj is self.primary_lbl
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            self.mpn_clicked.emit(self._row.key)
            return True
        return super().eventFilter(obj, event)

    def set_view(self, new_view: ChecklistRowView) -> None:
        if new_view.key != self._row.key:
            return
        self._row = new_view
        self._apply_view(self._row)

    def revert(self) -> None:
        self._apply_view(self._row)

    def _on_toggled(self, checked: bool) -> None:
        if self._ignore_signals:
            return
        self.checkbox.setEnabled(False)
        self.toggle_requested.emit(self._row.key, checked)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._row.key.kind == ChecklistRowKind.NOTES:
            super().mousePressEvent(event)
            return

        pos = event.position().toPoint()
        if (self.checkbox.geometry().contains(pos) or
            self.primary_lbl.geometry().contains(pos) or
            self.secondary_lbl.geometry().contains(pos) or
            (hasattr(self, 'find_number_lbl') and self.find_number_lbl.geometry().contains(pos)) or
            (hasattr(self, 'chip_strip') and self.chip_strip.geometry().contains(pos))):
            super().mousePressEvent(event)
        else:
            self.body_clicked.emit(self._row.key)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        for w in [self, self.primary_lbl, self.secondary_lbl]:
            w.style().unpolish(w)
            w.style().polish(w)

    def cleanup(self) -> None:
        if self._row.key.kind == ChecklistRowKind.THT:
            self.primary_lbl.removeEventFilter(self)
            for chip in self.chips.values():
                try: chip.clicked.disconnect()
                except Exception: pass
        try: self.refdes_chip_clicked.disconnect()
        except Exception: pass
        try: self.checkbox.toggled.disconnect()
        except Exception:
            logger.exception('Exception caught in checklist_row')
        try: self.toggle_requested.disconnect()
        except Exception:
            logger.exception('Exception caught in checklist_row')
        try: self.body_clicked.disconnect()
        except Exception:
            logger.exception('Exception caught in checklist_row')
        try: self.mpn_clicked.disconnect()
        except Exception:
            logger.exception('Exception caught in checklist_row')
