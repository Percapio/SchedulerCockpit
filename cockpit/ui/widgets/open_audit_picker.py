"""Open audit picker."""

from datetime import datetime, timezone, timedelta
from typing import Sequence
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget, QListWidgetItem, QLabel, QFrame
)

from cockpit.services.views import OpenAuditDigest
from cockpit.ui.widgets.toast import Toast  # Phase 3

PST = timezone(timedelta(hours=-8))

def format_updated_stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("updated_at must be timezone-aware")
    local = moment.astimezone(PST)
    hour12 = (local.hour % 12) or 12
    return f"{local:%Y-%m-%d}, {hour12}:{local:%M} {local:%p}"


class PickerRow(QFrame):
    selected = pyqtSignal(int)

    def __init__(self, digest: OpenAuditDigest, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "checklist-row")
        self._audit_id = digest.audit_id
        suffix = digest.split_suffix or ""
        cells = [
            QLabel(digest.part_number),
            QLabel(f"{digest.work_order_ref}{suffix}"),
            QLabel(f"qty {digest.quantity}"),
            QLabel(f"[{digest.status}]"),
            QLabel(f"updated {format_updated_stamp(digest.updated_at)}"),
        ]
        layout = QHBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)
        for c in cells:
            layout.addWidget(c)
        layout.addStretch()

    def mousePressEvent(self, event) -> None:
        self.selected.emit(self._audit_id)
        super().mousePressEvent(event)


class OpenAuditPicker(QWidget):
    audit_selected = pyqtSignal(int)
    new_audit_requested = pyqtSignal()
    font_scale_change_requested = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        
        layout.addLayout(self.build_title_row())
        
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)
        
        self.new_btn = QPushButton("+ New audit")
        self.new_btn.clicked.connect(self.new_audit_requested.emit)
        layout.addWidget(self.new_btn)

    def build_title_row(self) -> QHBoxLayout:
        title = QLabel("Select an Audit")
        title.setProperty("class", "h1")
        minus = QPushButton("A-")
        minus.clicked.connect(lambda: self.font_scale_change_requested.emit(-1))
        plus = QPushButton("A+")
        plus.clicked.connect(lambda: self.font_scale_change_requested.emit(1))
        
        layout = QHBoxLayout()
        layout.addWidget(title)
        layout.addStretch()
        layout.addWidget(minus)
        layout.addWidget(plus)
        return layout

    def populate(self, digests: Sequence[OpenAuditDigest]) -> None:
        self.list_widget.clear()
        for d in digests:
            row = PickerRow(d)
            item = QListWidgetItem(self.list_widget)
            item.setSizeHint(row.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, row)
            row.selected.connect(lambda aid, it=item: self._on_row_selected(it, aid))
            
        self.list_widget.scrollToTop()

    def _on_row_selected(self, item: QListWidgetItem, audit_id: int) -> None:
        self.list_widget.setCurrentItem(item)
        self.audit_selected.emit(audit_id)
