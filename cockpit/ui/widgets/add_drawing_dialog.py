"""Add drawing dialog."""

from pathlib import Path
from PyQt6.QtCore import Qt, pyqtSignal
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


DROP_PROMPT = "Drop PDF here"
BUSY_TEXT = "Reading the drawing…"


class AddDrawingDialog(QDialog):
    """
    Modal dialog hosting a single-file drop-zone for a .pdf.

    Phase 57 §9.2: a primary drawing is read on MainWindow's drawing worker, so a drop emits
    drawing_dropped and the dialog stays open and busy until MainWindow reports back, through
    accept() or show_error(). Cancel while busy emits cancel_requested, which sets the worker's stop
    flag, and closes the dialog. A secondary drawing has no labels and is still added here.
    """

    drawing_dropped = pyqtSignal(object)  # pathlib.Path
    cancel_requested = pyqtSignal()

    def __init__(
        self,
        ingestion_service: IngestionService,
        audit_id: int,
        parent: QWidget | None = None,
        *,
        secondary: bool = False,
    ) -> None:
        super().__init__(parent)
        self._ingestion_service = ingestion_service
        self._audit_id = audit_id
        self._secondary = secondary
        self._busy = False

        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Add Secondary Drawing" if secondary else "Add Drawing")
        self.setAcceptDrops(True)
        self.resize(400, 300)

        layout = QVBoxLayout(self)

        self._drop_zone = QLabel(DROP_PROMPT)
        self._drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_zone.setProperty("class", "drop-zone-label")
        layout.addWidget(self._drop_zone, stretch=1)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red; font-weight: bold;")
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.setWordWrap(True)
        # Phase 57 §10.5's rule for operator-visible text: never rich text.
        self._error_label.setTextFormat(Qt.TextFormat.PlainText)
        self._error_label.setVisible(False)
        layout.addWidget(self._error_label)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        layout.addWidget(self._cancel_btn)

    @property
    def audit_id(self) -> int:
        return self._audit_id

    @property
    def is_busy(self) -> bool:
        return self._busy

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._busy:
            return
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
        if self._busy:
            return
        url = event.mimeData().urls()[0]
        dropped_path = Path(url.toLocalFile())

        if not self._secondary:
            self.set_busy(True)
            self.drawing_dropped.emit(dropped_path)
            return

        try:
            self._ingestion_service.add_secondary_pdf_to_audit(self._audit_id, dropped_path)
            self.accept()
        except IngestionError as exc:
            logger.exception('Exception caught in add_drawing_dialog')
            self.show_error(str(exc))
        except PersistenceError as exc:
            logger.exception('Exception caught in add_drawing_dialog')
            self.show_error(f"Database error: {str(exc)}")

    def set_busy(self, busy: bool) -> None:
        """Busy while MainWindow's worker reads the drawing: drops are refused, Cancel stays live."""
        self._busy = busy
        self.setAcceptDrops(not busy)
        self._drop_zone.setText(BUSY_TEXT if busy else DROP_PROMPT)
        if busy:
            self._error_label.setVisible(False)

    def show_error(self, text: str) -> None:
        self.set_busy(False)
        self._error_label.setText(text)
        self._error_label.setVisible(True)

    def _on_cancel(self) -> None:
        if self._busy:
            self.cancel_requested.emit()
        self.reject()
