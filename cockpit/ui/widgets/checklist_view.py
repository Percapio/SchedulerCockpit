"""Checklist view widget."""

from PyQt6.QtCore import pyqtSignal, QEvent, Qt, QObject
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QLabel

from cockpit.services.views import ActiveAuditView, ChecklistRowView, ChecklistRowKey
from .checklist_row import ChecklistRow


class ChecklistView(QScrollArea):
    toggle_requested = pyqtSignal(object, bool)
    notes_commit_requested = pyqtSignal(object, object)
    empty_space_clicked = pyqtSignal()
    body_clicked = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.setWidget(self._container)
        
        self._index: dict[ChecklistRowKey, ChecklistRow] = {}
        
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (obj is self.viewport()
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            pos_in_container = self._container.mapFrom(
                self.viewport(), event.position().toPoint())
            last_row = self._last_row_widget_or_none()
            if last_row is None or pos_in_container.y() > last_row.geometry().bottom():
                self.empty_space_clicked.emit()
                return True
        return super().eventFilter(obj, event)

    def _last_row_widget_or_none(self) -> ChecklistRow | None:
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, ChecklistRow):
                return widget
        return None

    def populate(self, view: ActiveAuditView) -> None:
        # Clear existing
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        self._index.clear()
        
        # THT Section
        tht_header = QLabel(f"THT Verification ({len(view.tht_rows)} items)")
        tht_header.setProperty("class", "h2")
        self._layout.addWidget(tht_header)
        
        for row_view in view.tht_rows:
            row_widget = ChecklistRow(row_view)
            row_widget.toggle_requested.connect(self.toggle_requested.emit)
            row_widget.notes_commit_requested.connect(self.notes_commit_requested.emit)
            row_widget.body_clicked.connect(self.body_clicked.emit)
            self._layout.addWidget(row_widget)
            self._index[row_view.key] = row_widget
            
        # Notes Section
        notes_header = QLabel(f"Build Notes ({len(view.notes_rows)} items)")
        notes_header.setProperty("class", "h2")
        self._layout.addWidget(notes_header)
        
        for row_view in view.notes_rows:
            row_widget = ChecklistRow(row_view)
            row_widget.toggle_requested.connect(self.toggle_requested.emit)
            row_widget.notes_commit_requested.connect(self.notes_commit_requested.emit)
            row_widget.body_clicked.connect(self.body_clicked.emit)
            self._layout.addWidget(row_widget)
            self._index[row_view.key] = row_widget
            
        self._layout.addStretch()

    def update_row(self, updated: ChecklistRowView) -> None:
        if updated.key in self._index:
            self._index[updated.key].set_view(updated)

    def revert_row(self, row_key: ChecklistRowKey) -> None:
        if row_key in self._index:
            self._index[row_key].revert()

    def set_selected_row(self, row_key: ChecklistRowKey) -> None:
        for k, row_widget in self._index.items():
            row_widget.set_selected(k == row_key)

    def clear_selected_row(self) -> None:
        for row_widget in self._index.values():
            row_widget.set_selected(False)
