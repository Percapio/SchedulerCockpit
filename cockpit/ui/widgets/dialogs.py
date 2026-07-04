from datetime import date

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QMessageBox, QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QDateEdit, QLabel
)


class ShipDateDialog(QDialog):
    """Pick a ship date, or explicitly select None to clear it (Phase 32, 2.2).

    Usage:
        dlg = ShipDateDialog(current, parent)
        if dlg.exec():
            new_value = dlg.selected_date()  # date | None
    """

    def __init__(self, current: date | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ship Date")
        self.setModal(True)
        self._cleared = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a ship date:"))

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        seed = QDate(current.year, current.month, current.day) if current else QDate.currentDate()
        self.date_edit.setDate(seed)
        layout.addWidget(self.date_edit)

        buttons = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)

        none_btn = QPushButton("None")
        none_btn.setToolTip("Clear the ship date")
        none_btn.clicked.connect(self._on_none)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        buttons.addWidget(ok_btn)
        buttons.addWidget(none_btn)
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

    def _on_none(self) -> None:
        self._cleared = True
        self.accept()

    def selected_date(self) -> date | None:
        if self._cleared:
            return None
        qd = self.date_edit.date()
        return date(qd.year(), qd.month(), qd.day())

def confirm_destructive(title: str, body: str, confirm_label: str) -> bool:
    """
    Shows a modal dialog with a custom confirmation button and a Cancel button.
    The Cancel button is the default. Returns True only if the confirm button is clicked.
    """
    msg = QMessageBox()
    msg.setWindowTitle(title)
    msg.setText(body)
    msg.setIcon(QMessageBox.Icon.Warning)

    confirm_btn = msg.addButton(confirm_label, QMessageBox.ButtonRole.AcceptRole)
    cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    
    msg.setDefaultButton(cancel_btn)

    msg.exec()
    
    return msg.clickedButton() == confirm_btn
