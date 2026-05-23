"""Identity header."""

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton

from cockpit.services.views import ActiveAuditView


class IdentityHeader(QWidget):
    back_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.title_lbl = QLabel()
        self.title_lbl.setProperty("class", "h2")
        layout.addWidget(self.title_lbl)
        
        self.qty_lbl = QLabel()
        layout.addWidget(self.qty_lbl)
        
        self.status_lbl = QLabel()
        layout.addWidget(self.status_lbl)
        
        layout.addStretch()
        
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(self.back_requested.emit)
        layout.addWidget(back_btn)

    def set_audit(self, view: ActiveAuditView) -> None:
        suffix = view.split_suffix if view.split_suffix else ""
        self.title_lbl.setText(f"{view.part_number} — {view.work_order_ref}{suffix}")
        self.qty_lbl.setText(f"Qty: {view.quantity}")
        self.status_lbl.setText(f"Status: {view.status}")
