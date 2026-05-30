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

logger = logging.getLogger(__name__)

class AuditBomRow(QFrame):
    mpn_label_clicked = pyqtSignal(str)
    refdes_chip_clicked = pyqtSignal(str)
    
    def __init__(self, view: AuditBomRowView, theme: Theme) -> None:
        super().__init__()
        self.setProperty("class", "bom-grouping")
        self.setProperty("selected", False)
        self._mpn_value = view.component_mpn
        
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(0)
        
        # Find# Label
        self.find_number_badge = QLabel(str(view.find_number))
        self.find_number_badge.setProperty("class", "find-number-badge")
        self.find_number_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._find_cell = QFrame(self)
        self._find_cell.setFrameShape(QFrame.Shape.NoFrame)
        self._find_cell.setProperty("class", "cell-find")
        find_cell_layout = QVBoxLayout(self._find_cell)
        find_cell_layout.setContentsMargins(0, 0, 0, 0)
        find_cell_layout.addWidget(self.find_number_badge)
        
        # MPN Label
        self.mpn_label = QLabel(self._mpn_value)
        self.mpn_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.mpn_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.mpn_label.setProperty("class", "mpn-label")
        self.mpn_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mpn_label.customContextMenuRequested.connect(self._show_context_menu)
        self._mpn_filter = MPNLabelFilter(self.mpn_label, self)
        self.mpn_label.installEventFilter(self._mpn_filter)
        
        self._mpn_cell = QFrame(self)
        self._mpn_cell.setFrameShape(QFrame.Shape.NoFrame)
        self._mpn_cell.setProperty("class", "cell-mpn")
        mpn_cell_layout = QVBoxLayout(self._mpn_cell)
        mpn_cell_layout.setContentsMargins(0, 0, 0, 0)
        mpn_cell_layout.addWidget(self.mpn_label)
        
        # Description Label
        desc_text = view.description or ""
        self.desc_label = QLabel(desc_text)
        self.desc_label.setWordWrap(True)
        self.desc_label.setProperty("class", "desc-label")
        
        self._desc_cell = QFrame(self)
        self._desc_cell.setFrameShape(QFrame.Shape.NoFrame)
        self._desc_cell.setProperty("class", "cell-description")
        desc_cell_layout = QVBoxLayout(self._desc_cell)
        desc_cell_layout.setContentsMargins(0, 0, 0, 0)
        desc_cell_layout.addWidget(self.desc_label)
        
        # Chips container
        self.chip_strip = QWidget()
        flow_spacing = theme.bom_chip_flow_spacing()
        chip_layout = FlowLayout(
            self.chip_strip,
            margin=0,
            h_spacing=flow_spacing,
            v_spacing=flow_spacing,
        )
        
        self.chips: dict[str, RefDesChip] = {}
        for rd in view.ref_des_list:
            chip = RefDesChip(rd)
            chip.clicked.connect(self.refdes_chip_clicked.emit)
            chip_layout.addWidget(chip)
            self.chips[rd] = chip
            
        self._refdes_cell = QFrame(self)
        self._refdes_cell.setFrameShape(QFrame.Shape.NoFrame)
        self._refdes_cell.setProperty("class", "cell-refdes")
        refdes_cell_layout = QVBoxLayout(self._refdes_cell)
        refdes_cell_layout.setContentsMargins(0, 0, 0, 0)
        refdes_cell_layout.addWidget(self.chip_strip)
        
        grid.addWidget(self._find_cell, 0, 0)
        grid.addWidget(self._mpn_cell, 0, 1)
        grid.addWidget(self._refdes_cell, 0, 2)
        grid.addWidget(self._desc_cell, 1, 0, 1, 3)
        
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 3)

    def _show_context_menu(self, pos: Any) -> None:
        menu = QMenu(self)
        copy_action = QAction("Copy", self)
        copy_action.triggered.connect(self._copy_mpn)
        menu.addAction(copy_action)
        menu.exec(self.mpn_label.mapToGlobal(pos))

    def _copy_mpn(self) -> None:
        text = self.mpn_label.selectedText() or self._mpn_value
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)

    def set_mpn_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        for w in [self, self._mpn_cell, self.mpn_label, self._desc_cell, self.desc_label]:
            w.style().unpolish(w)
            w.style().polish(w)

    def set_refdes_selected(self, ref_des: str | None) -> None:
        for rd, chip in self.chips.items():
            is_sel = (rd == ref_des)
            chip.setProperty("selected", is_sel)
            chip.style().unpolish(chip)
            chip.style().polish(chip)

    def cleanup(self) -> None:
        self.mpn_label.removeEventFilter(self._mpn_filter)
        try: self.mpn_label.customContextMenuRequested.disconnect()
        except Exception:
            logger.exception('Exception caught in audit_bom_panel')
        for chip in self.chips.values():
            try: chip.clicked.disconnect()
            except Exception:
                logger.exception('Exception caught in audit_bom_panel')
        try: self.mpn_label_clicked.disconnect()
        except Exception:
            logger.exception('Exception caught in audit_bom_panel')
        try: self.refdes_chip_clicked.disconnect()
        except Exception:
            logger.exception('Exception caught in audit_bom_panel')


class AuditBomPanel(QScrollArea):
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

        self.setWidgetResizable(True)
        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        self.setWidget(self.container)

    def load(self, audit_id: int) -> None:
        self._clear_layout()
        self._selected_mpns.clear()
        self._selected_ref_des = None
        self._row_index.clear()

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
            self.layout.addWidget(lbl)
            return

        for view in views:
            row = AuditBomRow(view, self._theme)
            row.mpn_label_clicked.connect(self._on_mpn_label_clicked)
            row.refdes_chip_clicked.connect(self._on_refdes_chip_clicked)
            self.layout.addWidget(row)
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

    def _update_styles(self) -> None:
        for mpn, row in self._row_index.items():
            row.set_mpn_selected(mpn in self._selected_mpns)
            row.set_refdes_selected(self._selected_ref_des)

    def _clear_layout(self) -> None:
        prior_children = _drain_layout_widgets(self.layout)
        for child in prior_children:
            purge_widget_subtree(child)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.empty_space_clicked.emit()
        super().mousePressEvent(ev)
