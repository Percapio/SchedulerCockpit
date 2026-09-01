from datetime import date

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QMessageBox, QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QDateEdit, QLabel,
    QDoubleSpinBox, QWidget, QLineEdit
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

def confirm_destructive(title: str, body: str, confirm_label: str, parent: QWidget) -> bool:
    """
    Shows a modal dialog with a custom confirmation button and a Cancel button.
    The Cancel button is the default. Returns True only if the confirm button is clicked.
    """
    msg = QMessageBox(parent)
    msg.setWindowTitle(title)
    msg.setText(body)
    msg.setIcon(QMessageBox.Icon.Warning)

    confirm_btn = msg.addButton(confirm_label, QMessageBox.ButtonRole.AcceptRole)
    cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    
    msg.setDefaultButton(cancel_btn)

    msg.exec()
    
    return msg.clickedButton() == confirm_btn


class OpsPerBoardDialog(QDialog):
    """Pick an OPS per board time, or explicitly clear it.

    Usage:
        dlg = OpsPerBoardDialog(current, parent)
        if dlg.exec():
            new_value = dlg.result_value()  # float | None
    """

    def __init__(self, current: float | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OPS per Board")
        self.setModal(True)
        self._cleared = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Enter OPS time per board:"))

        self.spin_box = QDoubleSpinBox()
        self.spin_box.setSuffix(" min/board")
        self.spin_box.setRange(0.0, 240.0)
        self.spin_box.setDecimals(2)
        self.spin_box.setValue(current if current is not None else 0.0)
        layout.addWidget(self.spin_box)

        buttons = QHBoxLayout()
        ok_btn = QPushButton("Save")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Clear OPS per board (reset to blank)")
        clear_btn.clicked.connect(self._on_clear)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        buttons.addWidget(ok_btn)
        buttons.addWidget(clear_btn)
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

    def _on_clear(self) -> None:
        self._cleared = True
        self.accept()

    def was_cleared(self) -> bool:
        return self._cleared

    def result_value(self) -> float | None:
        if self._cleared:
            return None
        return float(self.spin_box.value())


class TypedConfirmationDialog(QDialog):
    def __init__(self, title: str, expected_audit_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        
        layout = QVBoxLayout(self)
        
        body = QLabel(
            f"You are about to delete {expected_audit_count} active audits.\n\n"
            "This will clear all audit data and remove their associated files. "
            "Holidays, schema version, settings, logs, and crash reports will be retained.\n\n"
            "To confirm, type 'DELETE' below:"
        )
        body.setWordWrap(True)
        layout.addWidget(body)
        
        self.input_field = QLineEdit()
        self.input_field.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.input_field)
        
        buttons = QHBoxLayout()
        buttons.addStretch()
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        
        self.confirm_btn = QPushButton("Confirm")
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.clicked.connect(self.accept)
        
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.confirm_btn)
        
        self.cancel_btn.setDefault(True)
        
        layout.addLayout(buttons)
        
    def _on_text_changed(self, text: str) -> None:
        self.confirm_btn.setEnabled(text == "DELETE")
