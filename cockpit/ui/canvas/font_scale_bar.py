from PyQt6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QLabel
from PyQt6.QtCore import pyqtSignal, Qt

class FontScaleBar(QWidget):
    scale_decrease_requested = pyqtSignal()
    scale_increase_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        
        self._btn_dec = QPushButton("-")
        self._btn_dec.setFixedWidth(24)
        self._btn_dec.clicked.connect(self.scale_decrease_requested.emit)
        
        self._lbl_percent = QLabel("100%")
        self._lbl_percent.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_percent.setMinimumWidth(45)
        
        self._btn_inc = QPushButton("+")
        self._btn_inc.setFixedWidth(24)
        self._btn_inc.clicked.connect(self.scale_increase_requested.emit)
        
        layout.addWidget(self._btn_dec)
        layout.addWidget(self._lbl_percent)
        layout.addWidget(self._btn_inc)
        
        self.setLayout(layout)

    def update_display(self, percentage: int) -> None:
        self._lbl_percent.setText(f"{percentage}%")
