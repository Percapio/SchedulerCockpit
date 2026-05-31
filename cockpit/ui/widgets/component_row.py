"""Shared component row UI core."""

from dataclasses import dataclass
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QCursor, QAction
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget, QMenu, QApplication
from typing import Any
import logging

from cockpit.ui.theme import Theme
from cockpit.ui.widgets.flow_layout import FlowLayout
from cockpit.ui.widgets.refdes_chip import RefDesChip, MPNLabelFilter

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ComponentRowFields:
    find_number: int | None
    mpn: str
    description: str | None
    ref_des_list: tuple[str, ...]

class ComponentRowCore(QFrame):
    mpn_label_clicked = pyqtSignal(str)
    refdes_chip_clicked = pyqtSignal(str)

    def __init__(self, view: ComponentRowFields, theme: Theme) -> None:
        super().__init__()
        self.setProperty("class", "component-card")
        self.setProperty("selected", False)
        self._mpn_value = view.mpn

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(0)

        # Find# Label
        self.find_number_badge = QLabel(str(view.find_number) if view.find_number is not None else "")
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
        except Exception: pass
        for chip in self.chips.values():
            try: chip.clicked.disconnect()
            except Exception: pass
        try: self.mpn_label_clicked.disconnect()
        except Exception: pass
        try: self.refdes_chip_clicked.disconnect()
        except Exception: pass
