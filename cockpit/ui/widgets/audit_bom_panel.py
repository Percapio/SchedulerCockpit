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
    mpn_label_clicked = pyqtSignal(str)
    refdes_chip_clicked = pyqtSignal(str)
    
    def __init__(self, view: AuditBomRowView, theme: Theme) -> None:
        super().__init__()
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
        self.core.mpn_label_clicked.connect(self.mpn_label_clicked.emit)
        self.core.refdes_chip_clicked.connect(self.refdes_chip_clicked.emit)
        layout.addWidget(self.core)

    def set_mpn_selected(self, selected: bool) -> None:
        self.core.set_mpn_selected(selected)

    def set_refdes_selected(self, ref_des: str | None) -> None:
        self.core.set_refdes_selected(ref_des)

    def cleanup(self) -> None:
        self.core.cleanup()
        try: self.mpn_label_clicked.disconnect()
        except Exception: pass
        try: self.refdes_chip_clicked.disconnect()
        except Exception: pass


class AuditBomPanel(QWidget):
    bom_mpn_toggled = pyqtSignal(str)
    bom_refdes_selected = pyqtSignal(str)
    empty_space_clicked = pyqtSignal()
    error_occurred = pyqtSignal(object)  # FailurePayload

    def __init__(self, layout_query_service: LayoutQueryService, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout_query_service = layout_query_service
        self._theme = theme
        self._selected_mpns: set[str] = set()
        self._selected_ref_des: str | None = None
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
        self._selected_mpns.clear()
        self._selected_ref_des = None
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
            row.mpn_label_clicked.connect(self._on_mpn_label_clicked)
            row.refdes_chip_clicked.connect(self._on_refdes_chip_clicked)
            self.container_layout.addWidget(row)
            self._row_index[view.component_mpn] = row

    def clear(self) -> None:
        self._selected_mpns.clear()
        self._selected_ref_des = None
        for row in self._row_index.values():
            row.set_mpn_selected(False)
            row.set_refdes_selected(None)

    def _on_mpn_label_clicked(self, mpn: str) -> None:
        if mpn in self._selected_mpns:
            self._selected_mpns.remove(mpn)
            self.bom_mpn_toggled.emit(mpn)
        else:
            self._selected_ref_des = None
            self._selected_mpns.add(mpn)
            self.bom_mpn_toggled.emit(mpn)
            
        self._update_styles()

    def _on_refdes_chip_clicked(self, ref_des: str) -> None:
        self._selected_mpns.clear()
        
        if self._selected_ref_des == ref_des:
            self._selected_ref_des = None
        else:
            self._selected_ref_des = ref_des
            
        self.bom_refdes_selected.emit(ref_des)
        self._update_styles()

    def select_refdes(self, ref_des: str) -> None:
        self._selected_mpns.clear()
        self._selected_ref_des = ref_des
        self._update_styles()

    def scroll_to_refdes(self, ref_des: str) -> None:
        row = next((r for r in self._row_index.values() if ref_des in r._view.ref_des_list), None)
        if row is not None:
            self.scroll.ensureWidgetVisible(row)

    def _update_styles(self) -> None:
        for mpn, row in self._row_index.items():
            row.set_mpn_selected(mpn in self._selected_mpns)
            row.set_refdes_selected(self._selected_ref_des)

    def _clear_layout(self) -> None:
        prior_children = _drain_layout_widgets(self.container_layout)
        for child in prior_children:
            purge_widget_subtree(child)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            import PyQt6.QtGui
            if isinstance(event, PyQt6.QtGui.QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                self.empty_space_clicked.emit()
        return super().eventFilter(obj, event)
