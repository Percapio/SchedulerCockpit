"""General Notes section widget."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QPushButton

class AuditNotesView(QWidget):
    notes_commit_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        header_layout = QHBoxLayout()
        self.header = QLabel("General Notes")
        self.header.setProperty("class", "section-header")
        header_layout.addWidget(self.header)
        
        header_layout.addStretch()
        
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.save_btn.setEnabled(False)
        header_layout.addWidget(self.save_btn)
        
        layout.addLayout(header_layout)
        
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Enter general notes for this audit here...")
        layout.addWidget(self.text_edit, stretch=1)
        
        # We can trigger save when focus is lost or text changes
        self.text_edit.focusOutEvent = self._on_focus_out
        self.text_edit.textChanged.connect(self._on_text_changed)
        
        self._ignore_signals = False
        self._last_saved_text = ""

    def _on_text_changed(self) -> None:
        if self._ignore_signals:
            return
        current_text = self.text_edit.toPlainText().strip()
        self.save_btn.setEnabled(current_text != self._last_saved_text)

    def _on_save_clicked(self) -> None:
        self.flush_pending()

    def _on_focus_out(self, event) -> None:
        QTextEdit.focusOutEvent(self.text_edit, event)
        self.flush_pending()

    def flush_pending(self) -> bool:
        if self._ignore_signals:
            return False
            
        current_text = self.text_edit.toPlainText().strip()
        if current_text != self._last_saved_text:
            self._last_saved_text = current_text
            self.save_btn.setEnabled(False)
            self.notes_commit_requested.emit(current_text if current_text else None)
            return True
        return False

    def populate(self, notes: str | None) -> None:
        self._ignore_signals = True
        try:
            text = notes or ""
            self.text_edit.setPlainText(text)
            self._last_saved_text = text
            self.save_btn.setEnabled(False)
        finally:
            self._ignore_signals = False
