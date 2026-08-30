from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import pyqtSignal
from typing import Optional
from cockpit.services.views import AuditIdentityBanner

class AuditIdentityBar(QWidget):
    back_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(self.back_requested.emit)
        layout.addWidget(self.back_btn)

        self.so_lbl = QLabel()
        layout.addWidget(self.so_lbl)
        
        self.pn_lbl = QLabel()
        self.pn_lbl.setProperty("class", "h2 hdr-part-number")
        layout.addWidget(self.pn_lbl)
        
        self.itar_lbl = QLabel("ITAR")
        self.itar_lbl.setProperty("class", "itar-badge")
        layout.addWidget(self.itar_lbl)
        
        self.qty_lbl = QLabel()
        layout.addWidget(self.qty_lbl)

        self.lt_lbl = QLabel()
        layout.addWidget(self.lt_lbl)

        self.class_lbl = QLabel()
        layout.addWidget(self.class_lbl)

        self.process_lbl = QLabel()
        layout.addWidget(self.process_lbl)

        self.customer_lbl = QLabel()
        layout.addWidget(self.customer_lbl)

        self.rowc_lbl = QLabel()
        layout.addWidget(self.rowc_lbl)

        self.status_lbl = QLabel()
        layout.addWidget(self.status_lbl)

        layout.addStretch()

        self.set_identity(None)

    def set_identity(self, data: Optional[AuditIdentityBanner]) -> None:
        if not data:
            self.so_lbl.setText("")
            self.pn_lbl.setText("")
            self.itar_lbl.hide()
            self.qty_lbl.setText("")
            self.lt_lbl.setText("")
            self.class_lbl.setText("")
            self.process_lbl.setText("")
            self.customer_lbl.setText("")
            self.rowc_lbl.setText("")
            self.status_lbl.setText("")
            return

        self.so_lbl.setText(f"{data.sales_order} \xb7" if data.sales_order else "")
        self.pn_lbl.setText(data.part_number)
        
        if data.is_itar:
            self.itar_lbl.show()
        else:
            self.itar_lbl.hide()
            
        self.qty_lbl.setText(f"\xb7 Qty: {data.quantity}" if data.quantity else "")
        self.lt_lbl.setText(f"\xb7 LT: {data.lead_time_days}" if data.lead_time_days else "")
        self.class_lbl.setText(f"\xb7 {data.assembly_class}" if data.assembly_class else "")
        self.process_lbl.setText(f"\xb7 {data.process}" if data.process else "")
        self.customer_lbl.setText(f"\xb7 {data.customer}" if data.customer else "")
        self.rowc_lbl.setText(f"\xb7 {data.repeat_marker}" if data.repeat_marker else "")
        self.status_lbl.setText(f"\xb7 Status: {data.status}" if data.status else "")
