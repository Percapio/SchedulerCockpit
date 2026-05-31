"""Checklist row widget."""

from PyQt6.QtCore import pyqtSignal, Qt, QEvent, QObject
from PyQt6.QtGui import QMouseEvent, QCursor
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QCheckBox, QLabel, QLineEdit, QFrame

from cockpit.services.views import ChecklistRowView, ChecklistRowKey, ChecklistRowKind
from cockpit.ui.widgets.refdes_chip import RefDesChip
from cockpit.ui.widgets.flow_layout import FlowLayout
from cockpit.ui.widgets.component_row import ComponentRowCore, ComponentRowFields
from cockpit.ui.theme import Theme
import logging
logger = logging.getLogger(__name__)


class ChecklistRow(QFrame):
    toggle_requested = pyqtSignal(object, bool)
    body_clicked = pyqtSignal(object)
    mpn_clicked = pyqtSignal(object)

    def __init__(self, row: ChecklistRowView, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row = row
        self._theme = theme
        self._ignore_signals = False
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.checkbox = QCheckBox()
        self.checkbox.toggled.connect(self._on_toggled)
        layout.addWidget(self.checkbox)
        
        if row.key.kind == ChecklistRowKind.THT:
            self.setProperty("class", "checklist-row")
            fields = ComponentRowFields(
                find_number=row.find_number,
                mpn=row.primary_label,
                description=row.secondary_label,
                ref_des_list=row.ref_des_list
            )
            self.core = ComponentRowCore(fields, theme)
            self.core.mpn_label_clicked.connect(self._on_core_mpn_clicked)
            self.core.refdes_chip_clicked.connect(lambda _: self.body_clicked.emit(self._row.key))
            layout.addWidget(self.core, stretch=1)
        else:
            self.setProperty("class", "component-card checklist-row")
            self.primary_lbl = QLabel(row.primary_label)
            self.primary_lbl.setProperty("class", "bold")
            
            self.secondary_lbl = QLabel(row.secondary_label or "")
            self.secondary_lbl.setWordWrap(True)
            
            layout.addWidget(self.primary_lbl)
            layout.addWidget(self.secondary_lbl, stretch=1)
        
        self._apply_view(row)

    def _on_core_mpn_clicked(self, mpn: str) -> None:
        self.mpn_clicked.emit(self._row.key)

    def _apply_view(self, view: ChecklistRowView) -> None:
        self._ignore_signals = True
        try:
            self.checkbox.setChecked(view.is_verified)
            if view.key.kind == ChecklistRowKind.NOTES:
                self.primary_lbl.setText(view.primary_label)
                self.secondary_lbl.setText(view.secondary_label or "")
        finally:
            self._ignore_signals = False
            
        self.checkbox.setEnabled(True)

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
        if self.checkbox.geometry().contains(pos) or self.core.geometry().contains(pos):
            super().mousePressEvent(event)
        else:
            self.body_clicked.emit(self._row.key)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        if self._row.key.kind == ChecklistRowKind.THT:
            self.core.set_mpn_selected(selected)
            self.style().unpolish(self)
            self.style().polish(self)
        else:
            for w in [self, self.primary_lbl, self.secondary_lbl]:
                w.style().unpolish(w)
                w.style().polish(w)

    def cleanup(self) -> None:
        if self._row.key.kind == ChecklistRowKind.THT:
            self.core.cleanup()
        
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
