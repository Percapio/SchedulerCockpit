"""Add drawing dialog."""

from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cockpit.ingestion.service import IngestionService
from cockpit.ingestion.errors import IngestionError
from cockpit.persistence.errors import PersistenceError
import logging
logger = logging.getLogger(__name__)



class AddDrawingDialog(QDialog):
    """
    Modal dialog hosting a single-file drop-zone for a .pdf.
    """

    def __init__(
        self,
        ingestion_service: IngestionService,
        audit_id: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._ingestion_service = ingestion_service
        self._audit_id = audit_id
        
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Add Drawing")
        self.setAcceptDrops(True)
        self.resize(400, 300)
        
        layout = QVBoxLayout(self)
        
        self._drop_zone = QLabel("Drop PDF here")
        self._drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_zone.setProperty("class", "drop-zone-label")
        layout.addWidget(self._drop_zone, stretch=1)
        
        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red; font-weight: bold;")
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        layout.addWidget(self._error_label)
        
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        layout.addWidget(self._cancel_btn)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return
            
        urls = mime.urls()
        if len(urls) != 1:
            return
            
        url = urls[0]
        if not url.isLocalFile():
            return
            
        path = Path(url.toLocalFile())
        if path.suffix.lower() == ".pdf":
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        url = event.mimeData().urls()[0]
        dropped_path = Path(url.toLocalFile())
        
        try:
            self._ingestion_service.add_pdf_to_audit(self._audit_id, dropped_path)
            self.accept()
        except IngestionError as exc:
            logger.exception('Exception caught in add_drawing_dialog')
            self._error_label.setText(str(exc))
            self._error_label.setVisible(True)
        except PersistenceError as exc:
            logger.exception('Exception caught in add_drawing_dialog')
            self._error_label.setText(f"Database error: {str(exc)}")
            self._error_label.setVisible(True)

    def _on_cancel(self) -> None:
        self.reject()
