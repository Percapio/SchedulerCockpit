"""Open audit picker."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QListWidget, QListWidgetItem, QLabel
)

from cockpit.services.views import OpenAuditDigest
from cockpit.ui.widgets.toast import Toast  # Phase 3


class OpenAuditPicker(QWidget):
    audit_selected = pyqtSignal(int)
    new_audit_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        
        title = QLabel("Select an Audit")
        title.setProperty("class", "h1")
        layout.addWidget(title)
        
        self.list_widget = QListWidget()
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget)
        
        self.new_btn = QPushButton("+ New audit")
        self.new_btn.clicked.connect(self.new_audit_requested.emit)
        layout.addWidget(self.new_btn)

    def populate(self, digests: list[OpenAuditDigest]) -> None:
        self.list_widget.clear()
        for d in digests:
            suffix = d.split_suffix if d.split_suffix else ""
            display = f"{d.part_number}  {d.work_order_ref}{suffix}      qty {d.quantity}   [{d.status}]\nupdated {d.updated_at.isoformat()}"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, d.audit_id)
            self.list_widget.addItem(item)
            
        self.list_widget.scrollToTop()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        audit_id = item.data(Qt.ItemDataRole.UserRole)
        self.audit_selected.emit(audit_id)
