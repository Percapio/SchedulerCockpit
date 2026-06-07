from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
    QListWidget, QDateEdit, QLabel, QListWidgetItem
)
from PyQt6.QtCore import Qt, QDate
from datetime import date

class HolidayDialog(QDialog):
    def __init__(self, holiday_svc, parent=None):
        super().__init__(parent)
        self._svc = holiday_svc
        self.setWindowTitle("Global Holidays")
        self.setMinimumSize(300, 400)
        
        layout = QVBoxLayout(self)
        
        self.list_widget = QListWidget()
        layout.addWidget(QLabel("Non-Working Days:"))
        layout.addWidget(self.list_widget)
        
        add_layout = QHBoxLayout()
        self.date_picker = QDateEdit(QDate.currentDate())
        self.date_picker.setCalendarPopup(True)
        add_layout.addWidget(self.date_picker)
        
        self.btn_add = QPushButton("Add")
        self.btn_add.clicked.connect(self._on_add)
        add_layout.addWidget(self.btn_add)
        
        layout.addLayout(add_layout)
        
        self.btn_remove = QPushButton("Remove Selected")
        self.btn_remove.clicked.connect(self._on_remove)
        layout.addWidget(self.btn_remove)
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
        
        self._refresh()

    def _refresh(self):
        self.list_widget.clear()
        holidays = sorted(list(self._svc.list_holidays()))
        for h in holidays:
            item = QListWidgetItem(h.isoformat())
            item.setData(Qt.ItemDataRole.UserRole, h)
            self.list_widget.addItem(item)

    def _on_add(self):
        qdate = self.date_picker.date()
        d = date(qdate.year(), qdate.month(), qdate.day())
        self._svc.add_holiday(d)
        self._refresh()

    def _on_remove(self):
        selected = self.list_widget.selectedItems()
        if not selected:
            return
            
        for item in selected:
            d = item.data(Qt.ItemDataRole.UserRole)
            self._svc.remove_holiday(d)
            
        self._refresh()
