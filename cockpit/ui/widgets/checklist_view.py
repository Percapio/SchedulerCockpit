"""Checklist view widget."""

from PyQt6.QtCore import pyqtSignal, QEvent, Qt, QObject
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QLabel

from cockpit.services.views import ActiveAuditView, ChecklistRowView, ChecklistRowKey
from .checklist_row import ChecklistRow
from .qt_lifecycle import purge_widget_subtree, _drain_layout_widgets


class ChecklistView(QScrollArea):
    toggle_requested = pyqtSignal(object, bool)
    empty_space_clicked = pyqtSignal()
    body_clicked = pyqtSignal(object)
    mpn_clicked = pyqtSignal(object)
    refdes_chip_clicked = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(3)  # From theme.json left_panel.row.gutter_px
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

    def populate_section(self, views: list[ChecklistRowView], header_text: str) -> None:
        prior_children = _drain_layout_widgets(self._layout)
        for child in prior_children:
            purge_widget_subtree(child)
                
        self._index.clear()
        
        header = QLabel(header_text)
        header.setProperty("class", "section-header")
        self._layout.addWidget(header)
        
        for row_view in views:
            row_widget = ChecklistRow(row_view)
            row_widget.toggle_requested.connect(self.toggle_requested.emit)
            row_widget.body_clicked.connect(self.body_clicked.emit)
            row_widget.mpn_clicked.connect(self.mpn_clicked.emit)
            row_widget.refdes_chip_clicked.connect(self.refdes_chip_clicked.emit)
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
