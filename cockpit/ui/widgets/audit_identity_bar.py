import html

from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import pyqtSignal
from typing import Optional
from cockpit.services.views import AuditIdentityBanner


def _coloured(text: str, color: str) -> str:
    """One escaped run wrapped in a colour span.

    Every value reaching this function originates in a traveler workbook on a
    share other people write to. Escaping is what keeps a value containing
    markup from being parsed as markup once the label switches to rich text.
    """
    return f'<span style="color:{color};">{html.escape(text)}</span>'


def _class_markup(assembly_class: str, color: str) -> str:
    """Renders "Class 3" with only the digit coloured, any other class plain."""
    escaped = html.escape(assembly_class)
    if not assembly_class.endswith("3"):
        return escaped
    head = html.escape(assembly_class[:-1])
    return f'{head}<span style="color:{color};">3</span>'


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
        from cockpit.ui import facelift

        if data.assembly_class:
            self.class_lbl.setText(
                "\xb7 " + _class_markup(data.assembly_class, facelift.palette().overdue)
            )
        else:
            self.class_lbl.setText("")

        self.process_lbl.setText(f"\xb7 {data.process}" if data.process else "")
        self.customer_lbl.setText(f"\xb7 {data.customer}" if data.customer else "")

        marker = data.repeat_marker
        if marker.label or marker.reference:
            runs = []
            if marker.label:
                runs.append(html.escape(marker.label))
            if marker.reference:
                runs.append(_coloured(marker.reference, facelift.palette().attention))
            self.rowc_lbl.setText("\xb7 " + " ".join(runs))
        else:
            self.rowc_lbl.setText("")
        self.status_lbl.setText(f"\xb7 Status: {data.status}" if data.status else "")
