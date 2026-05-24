"""Page switcher segmented control."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QPushButton


class PageSwitcher(QWidget):
    """Segmented control to switch between PDF pages."""
    
    page_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.buttons: list[QPushButton] = []
        self._current_index: int = 0

    def set_page_count(self, count: int) -> None:
        """Rebuild the segments based on the page count."""
        # Clear existing buttons
        for btn in self.buttons:
            self.layout.removeWidget(btn)
            btn.deleteLater()
        self.buttons.clear()
        
        if count <= 1:
            self.hide()
            return
            
        labels = ["Top", "Bottom"]
        for i in range(count):
            label = labels[i] if i < len(labels) else f"Page {i+1}"
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, idx=i: self._on_button_clicked(idx))
            self.layout.addWidget(btn)
            self.buttons.append(btn)
            
        self._current_index = 0
        self.buttons[0].setChecked(True)
        self.show()

    def _on_button_clicked(self, index: int) -> None:
        if index == self._current_index:
            self.buttons[index].setChecked(True)  # prevent unchecking active
            return
            
        self.buttons[self._current_index].setChecked(False)
        self.buttons[index].setChecked(True)
        self._current_index = index
        
        self.page_changed.emit(index)
