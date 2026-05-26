"""Checklist row widget."""

from PyQt6.QtCore import pyqtSignal, Qt, QEvent, QObject
from PyQt6.QtGui import QMouseEvent, QCursor
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QCheckBox, QLabel, QLineEdit

from cockpit.services.views import ChecklistRowView, ChecklistRowKey, ChecklistRowKind


class ChecklistRow(QWidget):
    toggle_requested = pyqtSignal(object, bool)
    body_clicked = pyqtSignal(object)
    mpn_clicked = pyqtSignal(object)

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
            layout.addWidget(self.primary_lbl, stretch=35)
            layout.addWidget(self.secondary_lbl, stretch=65)
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
            self.secondary_lbl.geometry().contains(pos)):
            super().mousePressEvent(event)
        else:
            self.body_clicked.emit(self._row.key)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def cleanup(self) -> None:
        if self._row.key.kind == ChecklistRowKind.THT:
            self.primary_lbl.removeEventFilter(self)
        try: self.checkbox.toggled.disconnect()
        except: pass
        try: self.toggle_requested.disconnect()
        except: pass
        try: self.body_clicked.disconnect()
        except: pass
        try: self.mpn_clicked.disconnect()
        except: pass
