"""Audit BOM Side Panel widget."""

from PyQt6.QtCore import pyqtSignal, Qt, QEvent, QObject
from PyQt6.QtGui import QMouseEvent, QCursor, QAction
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QMenu, QApplication,
    QFrame, QGridLayout
)
from typing import Any
import logging

from cockpit.services.layout_query import LayoutQueryService, AuditBomRowView
from cockpit.persistence.errors import PersistenceError
from cockpit.ui.theme import Theme
from cockpit.ui.widgets.flow_layout import FlowLayout
from .qt_lifecycle import purge_widget_subtree, _drain_layout_widgets
from cockpit.ui.widgets.refdes_chip import RefDesChip, MPNLabelFilter

from cockpit.ui.widgets.component_row import ComponentRowCore, ComponentRowFields

logger = logging.getLogger(__name__)

class AuditBomRow(QFrame):
    row_clicked = pyqtSignal(str)
    
    def __init__(self, view: AuditBomRowView, theme: Theme) -> None:
        super().__init__()
        self.setProperty("class", "bom-grouping")
        self._view = view
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        fields = ComponentRowFields(
            find_number=view.find_number,
            mpn=view.component_mpn,
            description=view.description,
            ref_des_list=view.ref_des_list
        )
        self.core = ComponentRowCore(fields, theme)
        self.core.mpn_label_clicked.connect(lambda _: self.row_clicked.emit(self._view.component_mpn))
        self.core.refdes_chip_clicked.connect(lambda _: self.row_clicked.emit(self._view.component_mpn))
        layout.addWidget(self.core)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.row_clicked.emit(self._view.component_mpn)
        super().mousePressEvent(event)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.core.set_mpn_selected(selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def cleanup(self) -> None:
        self.core.cleanup()
        try: self.row_clicked.disconnect()
        except Exception: pass


class AuditBomPanel(QWidget):
    bom_row_clicked = pyqtSignal(str)
    empty_space_clicked = pyqtSignal()
    error_occurred = pyqtSignal(object)  # FailurePayload

    def __init__(self, layout_query_service: LayoutQueryService, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout_query_service = layout_query_service
        self._theme = theme
        self._selected_mpn: str | None = None
        self._row_index: dict[str, AuditBomRow] = {}

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        self.header_label = QLabel()
        self.header_label.setProperty("class", "bom-sticky-header")
        self.layout.addWidget(self.header_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.viewport().installEventFilter(self)
        
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 0, 0)
        self.container_layout.setSpacing(0)
        self.container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        self.scroll.setWidget(self.container)
        self.layout.addWidget(self.scroll)

    def load(self, audit_id: int) -> None:
        self._clear_layout()
        self._selected_mpn = None
        self._row_index.clear()
        self.header_label.setText("")

        try:
            views = self._layout_query_service.list_bom_rows_for_audit(audit_id)
        except PersistenceError as e:
            logger.exception('Exception caught in audit_bom_panel')
            from cockpit.services.exceptions import FailurePayload
            self.error_occurred.emit(FailurePayload("Failed to load BOM", e))
            return
            
        if not views:
            lbl = QLabel("No BOM ingested for this audit")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setProperty("class", "empty-bom-label")
            self.container_layout.addWidget(lbl)
            return

        unique_mpns = len(views)
        total_placements = sum(len(v.ref_des_list) for v in views)
        self.header_label.setText(f"SMT - Unique MPNs: {unique_mpns} | Total Placements: {total_placements}")

        for view in views:
            row = AuditBomRow(view, self._theme)
            row.row_clicked.connect(self.bom_row_clicked.emit)
            self.container_layout.addWidget(row)
            self._row_index[view.component_mpn] = row

    def clear(self) -> None:
        self._selected_mpn = None
        for row in self._row_index.values():
            row.set_selected(False)

    def select_mpn(self, mpn: str) -> None:
        self._selected_mpn = mpn
        self._update_styles()

    def scroll_to_mpn(self, mpn: str) -> None:
        if mpn in self._row_index:
            self.scroll.ensureWidgetVisible(self._row_index[mpn])

    def _update_styles(self) -> None:
        for mpn, row in self._row_index.items():
            row.set_selected(mpn == self._selected_mpn)

    def _clear_layout(self) -> None:
        prior_children = _drain_layout_widgets(self.container_layout)
        for child in prior_children:
            purge_widget_subtree(child)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (obj is self.scroll.viewport()
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            pos_in_container = self.container.mapFrom(
                self.scroll.viewport(), event.position().toPoint())
            last_row = self._last_row_widget_or_none()
            if last_row is None or pos_in_container.y() > last_row.geometry().bottom():
                self.empty_space_clicked.emit()
                return True
        return super().eventFilter(obj, event)

    def _last_row_widget_or_none(self) -> AuditBomRow | None:
        for i in reversed(range(self.container_layout.count())):
            item = self.container_layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, AuditBomRow):
                return widget
        return None
