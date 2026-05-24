"""Audit BOM Side Panel widget."""

from PyQt6.QtCore import pyqtSignal, Qt, QEvent, QObject
from PyQt6.QtGui import QMouseEvent, QCursor, QAction
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QMenu, QApplication
)
from typing import Any
import logging

from cockpit.services.layout_query import LayoutQueryService, AuditBomRowView
from cockpit.persistence.errors import PersistenceError

logger = logging.getLogger(__name__)

class MPNLabelFilter(QObject):
    def __init__(self, parent: Any, row: 'AuditBomRow') -> None:
        super().__init__(parent)
        self.row = row

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            assert isinstance(event, QMouseEvent)
            if event.button() == Qt.MouseButton.LeftButton:
                self.row.mpn_label_clicked.emit(self.row._mpn_value)
                return False  # Let Qt still handle selection
        return False

class RefDesChip(QLabel):
    clicked = pyqtSignal(str)

    def __init__(self, ref_des: str) -> None:
        super().__init__(ref_des)
        self._ref_des = ref_des
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet("""
            QLabel {
                background-color: #2D2D2D;
                color: #CCCCCC;
                border-radius: 3px;
                padding: 2px 4px;
                margin: 2px;
            }
            QLabel[selected="true"] {
                background-color: #007ACC;
                color: white;
                font-weight: bold;
            }
        """)
        self.setProperty("selected", False)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._ref_des)
        super().mousePressEvent(ev)


class AuditBomRow(QWidget):
    mpn_label_clicked = pyqtSignal(str)
    refdes_chip_clicked = pyqtSignal(str)

    def __init__(self, view: AuditBomRowView) -> None:
        super().__init__()
        self._mpn_value = view.component_mpn
        
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(4, 4, 4, 4)
        
        # MPN Label
        self.mpn_label = QLabel(self._mpn_value)
        self.mpn_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | 
            Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.mpn_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.mpn_label.setProperty("class", "mpn-cell")
        self.mpn_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mpn_label.customContextMenuRequested.connect(self._show_context_menu)
        self.mpn_label.setStyleSheet("""
            QLabel[class="mpn-cell"] {
                font-weight: bold;
                min-width: 80px;
            }
        """)
        
        self._mpn_filter = MPNLabelFilter(self.mpn_label, self)
        self.mpn_label.installEventFilter(self._mpn_filter)
        
        # Description Label
        desc = view.description or ""
        if view.mount_type:
            desc = f"[{view.mount_type}] {desc}"
        self.desc_label = QLabel(desc)
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        
        # Chips container
        self.chip_strip = QWidget()
        chip_layout = QHBoxLayout(self.chip_strip)
        chip_layout.setContentsMargins(0, 0, 0, 0)
        chip_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        
        self.chips: dict[str, RefDesChip] = {}
        for rd in view.ref_des_list:
            chip = RefDesChip(rd)
            chip.clicked.connect(self.refdes_chip_clicked.emit)
            chip_layout.addWidget(chip)
            self.chips[rd] = chip
            
        self.layout.addWidget(self.mpn_label, stretch=2)
        self.layout.addWidget(self.desc_label, stretch=3)
        self.layout.addWidget(self.chip_strip, stretch=4)
        
        self.setStyleSheet("""
            AuditBomRow {
                background-color: transparent;
                border-bottom: 1px solid #333333;
            }
            AuditBomRow[selected="true"] {
                background-color: #2A3642;
            }
        """)
        self.setProperty("selected", False)

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
        self.style().unpolish(self)
        self.style().polish(self)

    def set_refdes_selected(self, ref_des: str | None) -> None:
        for rd, chip in self.chips.items():
            is_sel = (rd == ref_des)
            chip.setProperty("selected", is_sel)
            chip.style().unpolish(chip)
            chip.style().polish(chip)


class AuditBomPanel(QScrollArea):
    bom_mpn_toggled = pyqtSignal(str)
    bom_refdes_selected = pyqtSignal(str)
    empty_space_clicked = pyqtSignal()
    error_occurred = pyqtSignal(object)  # FailurePayload

    def __init__(self, layout_query_service: LayoutQueryService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout_query_service = layout_query_service
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
            from cockpit.services.exceptions import FailurePayload
            self.error_occurred.emit(FailurePayload("Failed to load BOM", e))
            return
            
        if not views:
            lbl = QLabel("No BOM ingested for this audit")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color: #888888; padding: 20px;")
            self.layout.addWidget(lbl)
            return

        for view in views:
            row = AuditBomRow(view)
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
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.empty_space_clicked.emit()
        super().mousePressEvent(ev)
