"""Modal error dialog."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, 
    QPushButton, QHBoxLayout, QHeaderView, QWidget
)

from cockpit.ui.error_messages import FailurePayload


class ErrorDialog(QDialog):
    def __init__(self, payload: FailurePayload, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(payload.title)
        self.resize(500, 400)
        
        layout = QVBoxLayout(self)
        
        # 2. Summary
        summary_label = QLabel(payload.summary)
        summary_label.setWordWrap(True)
        summary_label.setObjectName("ErrorSummary")
        layout.addWidget(summary_label)
        
        layout.addSpacing(10)
        
        # 3. Detail panel
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setRowCount(len(payload.detail))
        self.table.setHorizontalHeaderLabels(["Field", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        
        for row, (key, value) in enumerate(payload.detail):
            key_item = QTableWidgetItem(key)
            val_item = QTableWidgetItem(value)
            self.table.setItem(row, 0, key_item)
            self.table.setItem(row, 1, val_item)
            
        layout.addWidget(self.table)
        
        # 4. Footer
        footer_text = payload.exception_class
        if payload.reason_code:
            footer_text += f" ({payload.reason_code})"
            
        footer_label = QLabel(footer_text)
        footer_label.setObjectName("ErrorFooter")
        footer_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(footer_label)
        
        # 5. OK Button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.ok_btn)
        
        layout.addLayout(btn_layout)
