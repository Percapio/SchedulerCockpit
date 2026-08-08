from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QMouseEvent, QCursor
from PyQt6.QtWidgets import QLabel


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
            ev.accept()
            return
        super().mousePressEvent(ev)
