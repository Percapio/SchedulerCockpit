from PyQt6.QtCore import pyqtSignal, Qt, QEvent, QObject
from PyQt6.QtGui import QMouseEvent, QCursor
from PyQt6.QtWidgets import QLabel
from typing import Any

class MPNLabelFilter(QObject):
    def __init__(self, parent: Any, row: Any) -> None:
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
    """
    A single Reference Designator chip.
    """
    clicked = pyqtSignal(str)
    
    def __init__(self, ref_des: str) -> None:
        super().__init__(ref_des)
        self._ref_des = ref_des
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setProperty("class", "refdes-chip")
        self.setProperty("selected", False)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._ref_des)
        super().mousePressEvent(ev)
