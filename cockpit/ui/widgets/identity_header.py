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
        
        self.itar_lbl = QLabel()
        self.itar_lbl.setStyleSheet("color: red; font-weight: bold;")
        self.itar_lbl.hide()
        layout.addWidget(self.itar_lbl)
        
        self.qty_lbl = QLabel()
        layout.addWidget(self.qty_lbl)
        
        self.status_lbl = QLabel()
        layout.addWidget(self.status_lbl)
        
        layout.addSpacing(20)

        layout.addStretch()
        
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(self.back_requested.emit)
        layout.addWidget(back_btn)

        self._current_view = None

    def set_audit(self, view: ActiveAuditView) -> None:
        self._current_view = view
        suffix = view.split_suffix if view.split_suffix else ""
        metadata = view.traveler_metadata or {}
        sales_order = metadata.get("sales_order_number", view.work_order_ref)
        self.title_lbl.setText(f"{view.part_number}{suffix}")
        
        itar_val = metadata.get("itar_classification")
        if itar_val == "ITAR":
            self.itar_lbl.setText("[ITAR]")
            self.itar_lbl.show()
        else:
            self.itar_lbl.hide()
            
        self.qty_lbl.setText(f"Qty: {view.quantity}")
        self.status_lbl.setText(f"Status: {view.status}")
