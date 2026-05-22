"""DropArea widget."""

import pathlib

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QDragLeaveEvent
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel


class DropArea(QWidget):
    drop_received = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setProperty("state", "resting")
        
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.main_label = QLabel("Drop the three files for this audit here")
        self.main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_label.setObjectName("DropAreaMainLabel")
        
        self.sub_label = QLabel("(\"Audit BOM\", \"Traveler\", and a .docx)")
        self.sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub_label.setObjectName("DropAreaSubLabel")
        
        layout.addWidget(self.main_label)
        layout.addWidget(self.sub_label)

    def _has_local_files(self, event: QDragEnterEvent | QDropEvent) -> bool:
        if not event.mimeData().hasUrls():
            return False
        urls = event.mimeData().urls()
        if not urls:
            return False
        return all(url.isLocalFile() for url in urls)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._has_local_files(event):
            event.acceptProposedAction()
            self.setProperty("state", "active")
            self.style().unpolish(self)
            self.style().polish(self)
            self.main_label.setText("Release to ingest")
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.setProperty("state", "resting")
        self.style().unpolish(self)
        self.style().polish(self)
        self.main_label.setText("Drop the three files for this audit here")

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("state", "resting")
        self.style().unpolish(self)
        self.style().polish(self)
        self.main_label.setText("Drop the three files for this audit here")
        
        if self._has_local_files(event):
            urls = event.mimeData().urls()
            paths = [pathlib.Path(url.toLocalFile()) for url in urls]
            if paths:
                self.drop_received.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.EnabledChange:
            if not self.isEnabled():
                self.setProperty("state", "disabled")
            else:
                self.setProperty("state", "resting")
            self.style().unpolish(self)
            self.style().polish(self)
