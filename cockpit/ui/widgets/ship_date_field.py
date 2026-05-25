"""Ship Date field widget."""

from datetime import date

from PyQt6.QtCore import QDate, pyqtSignal as Signal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QDateEdit, QWidget


class ShipDateField(QWidget):
    """An editable date field for ship_date that supports a 'Not Set' empty state."""
    commit_requested = Signal(object)  # emits date | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._label = QLabel("Ship Date:")
        layout.addWidget(self._label)
        
        self._editor = QDateEdit()
        self._editor.setCalendarPopup(True)
        self._editor.setSpecialValueText("Not Set")
        self._editor.setMinimumDate(QDate(2000, 1, 1))
        
        layout.addWidget(self._editor)
        
        self._editor.dateChanged.connect(self._on_date_changed)

    def set_value(self, d: date | None) -> None:
        """Hydrate the widget with a value without emitting the signal."""
        self.blockSignals(True)
        try:
            if d is None:
                self._editor.setDate(self._editor.minimumDate())
            else:
                self._editor.setDate(QDate(d.year, d.month, d.day))
        finally:
            self.blockSignals(False)

    def _on_date_changed(self, qdate: QDate) -> None:
        if qdate == self._editor.minimumDate():
            self.commit_requested.emit(None)
        else:
            self.commit_requested.emit(date(qdate.year(), qdate.month(), qdate.day()))
