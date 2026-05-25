"""General Notes section widget."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTextEdit

class AuditNotesView(QWidget):
    notes_commit_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.header = QLabel("General Notes")
        self.header.setProperty("class", "section-header")
        layout.addWidget(self.header)
        
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Enter general notes for this audit here...")
        layout.addWidget(self.text_edit, stretch=1)
        
        # We can trigger save when focus is lost
        self.text_edit.focusOutEvent = self._on_focus_out
        
        self._ignore_signals = False
        self._last_saved_text = ""

    def _on_focus_out(self, event) -> None:
        QTextEdit.focusOutEvent(self.text_edit, event)
        if self._ignore_signals:
            return
            
        current_text = self.text_edit.toPlainText().strip()
        if current_text != self._last_saved_text:
            self._last_saved_text = current_text
            self.notes_commit_requested.emit(current_text if current_text else None)

    def populate(self, notes: str | None) -> None:
        self._ignore_signals = True
        try:
            text = notes or ""
            self.text_edit.setPlainText(text)
            self._last_saved_text = text
        finally:
            self._ignore_signals = False
