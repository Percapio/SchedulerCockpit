"""Empty canvas placeholder widget."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel


class EmptyCanvasPlaceholder(QWidget):
    """Placeholder shown when no PDF is attached or when rendering fails."""

    def __init__(self, text: str = "No assembly drawing attached", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label = QLabel(text)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setObjectName("EmptyCanvasLabel")
        
        layout.addWidget(self.label)

    def set_text(self, text: str) -> None:
        """Update the placeholder text."""
        self.label.setText(text)
