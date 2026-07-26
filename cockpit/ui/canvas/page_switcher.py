"""Page switcher segmented control."""

from enum import Enum, auto
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QLabel
from cockpit.ui.theme import Theme


class SwitcherMode(Enum):
    HIDDEN = auto()
    SEGMENTED = auto()
    PAGER = auto()


class PageSwitcher(QWidget):
    """Segmented control or pager to switch between PDF pages."""
    
    page_changed = pyqtSignal(int)

    def __init__(self, theme: Theme | None = None, parent: QWidget | None = None) -> None:
        if isinstance(theme, QWidget) and parent is None:
            # Handle backward compatibility if instantiated as PageSwitcher(parent)
            parent = theme
            theme = None
        super().__init__(parent)
        self._theme = theme if theme is not None else Theme.for_testing()
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self._mode: SwitcherMode = SwitcherMode.HIDDEN
        self._page_count: int = 0
        self._current_index: int = 0
        self._segment_buttons: list[QPushButton] = []
        self._pager_prev: QPushButton | None = None
        self._pager_next: QPushButton | None = None
        self._pager_label: QLabel | None = None
        self._other_page_indicator_visible: bool = False

    @property
    def mode(self) -> SwitcherMode:
        return self._mode

    @property
    def buttons(self) -> list[QPushButton]:
        return self._segment_buttons

    def teardown_children(self) -> None:
        """Remove and delete all child widgets in layout."""
        while self.layout.count() > 0:
            item = self.layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._segment_buttons.clear()
        self._pager_prev = None
        self._pager_next = None
        self._pager_label = None

    def set_page_count(self, count: int, *, is_reference: bool = False) -> None:
        """Rebuild the segments or pager based on the page count."""
        self.teardown_children()
        self._page_count = count
        
        if count <= 1:
            self._mode = SwitcherMode.HIDDEN
            self._current_index = 0
            self.hide()
            return
            
        max_segmented = self._theme.page_switcher_segmented_max()
        if count <= max_segmented:
            self._mode = SwitcherMode.SEGMENTED
            if not is_reference and count == 2:
                labels = ["Top", "Bottom"]
            else:
                labels = [f"Page {i+1}" for i in range(count)]
                
            for i, label in enumerate(labels):
                btn = QPushButton(label)
                btn.setCheckable(True)
                btn.clicked.connect(lambda checked, idx=i: self._on_button_clicked(idx))
                self.layout.addWidget(btn)
                self._segment_buttons.append(btn)
                
            self._current_index = 0
            if self._segment_buttons:
                self._segment_buttons[0].setChecked(True)
            self.show()
        else:
            self._mode = SwitcherMode.PAGER
            self._pager_prev = QPushButton("◄")
            self._pager_prev.clicked.connect(self._step_prev)
            self.layout.addWidget(self._pager_prev)
            
            self._pager_label = QLabel()
            self._pager_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout.addWidget(self._pager_label)
            
            self._pager_next = QPushButton("►")
            self._pager_next.clicked.connect(self._step_next)
            self.layout.addWidget(self._pager_next)
            
            self._current_index = 0
            self._update_pager_display()
            self.show()

    def _on_button_clicked(self, index: int) -> None:
        if index == self._current_index:
            self._segment_buttons[index].setChecked(True)  # prevent unchecking active
            return
            
        self._segment_buttons[self._current_index].setChecked(False)
        self._segment_buttons[index].setChecked(True)
        self._current_index = index
        
        self._segment_buttons[index].setProperty("indicator", False)
        self._segment_buttons[index].style().unpolish(self._segment_buttons[index])
        self._segment_buttons[index].style().polish(self._segment_buttons[index])
        
        self.page_changed.emit(index)

    def _step_prev(self) -> None:
        if self._current_index > 0:
            self._current_index -= 1
            self._update_pager_display()
            self.page_changed.emit(self._current_index)

    def _step_next(self) -> None:
        if self._current_index < self._page_count - 1:
            self._current_index += 1
            self._update_pager_display()
            self.page_changed.emit(self._current_index)

    def _update_pager_display(self) -> None:
        if self._pager_label is not None:
            self._pager_label.setText(f"Page {self._current_index + 1} / {self._page_count}")
        if self._pager_prev is not None:
            self._pager_prev.setEnabled(self._current_index > 0)
        if self._pager_next is not None:
            self._pager_next.setEnabled(self._current_index < self._page_count - 1)

    def set_other_page_indicator(self, visible: bool) -> None:
        self._other_page_indicator_visible = visible
        if self._mode != SwitcherMode.SEGMENTED or len(self._segment_buttons) != 2:
            return

        other_index = 1 if self._current_index == 0 else 0
        self._segment_buttons[other_index].setProperty("indicator", visible)
        self._segment_buttons[other_index].style().unpolish(self._segment_buttons[other_index])
        self._segment_buttons[other_index].style().polish(self._segment_buttons[other_index])

        # Make sure the current index does not have it
        self._segment_buttons[self._current_index].setProperty("indicator", False)
        self._segment_buttons[self._current_index].style().unpolish(self._segment_buttons[self._current_index])
        self._segment_buttons[self._current_index].style().polish(self._segment_buttons[self._current_index])
