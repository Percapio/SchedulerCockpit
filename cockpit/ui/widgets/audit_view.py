"""Audit view container."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QSplitter

from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService
from cockpit.services.completion import CompletionService
from cockpit.services.audit_metadata import AuditMetadataService
from cockpit.services.layout_query import LayoutQueryService
from cockpit.layout.renderer import PdfRenderer
from cockpit.ui.widgets.dashboard import Dashboard
from cockpit.ui.widgets.layout_canvas import LayoutCanvas


class AuditView(QWidget):
    """QSplitter container for the Dashboard and LayoutCanvas."""
    
    exit_requested = pyqtSignal()
    error_occurred = pyqtSignal(object)  # FailurePayload

    def __init__(
        self,
        checklist_service: ChecklistService,
        split_service: AuditSplitService,
        completion_service: CompletionService,
        audit_metadata_service: AuditMetadataService,
        layout_query_service: LayoutQueryService,
        pdf_renderer: PdfRenderer,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        
        self._dashboard = Dashboard(
            checklist_service=checklist_service,
            split_service=split_service,
            completion_service=completion_service,
            audit_metadata_service=audit_metadata_service,
            parent=self._splitter
        )
        
        self._layout_canvas = LayoutCanvas(
            layout_query_service=layout_query_service,
            pdf_renderer=pdf_renderer,
            parent=self._splitter
        )
        
        self._splitter.addWidget(self._dashboard)
        self._splitter.addWidget(self._layout_canvas)
        
        layout.addWidget(self._splitter)
        
        # Signal wiring
        self._dashboard.exit_requested.connect(self.exit_requested.emit)
        self._dashboard.error_occurred.connect(self.error_occurred.emit)
        self._layout_canvas.error_occurred.connect(self.error_occurred.emit)
        
        self._first_show = True

    def load(self, audit_id: int) -> None:
        """Load the audit identified by audit_id into both panes."""
        self._dashboard.load(audit_id)
        self._layout_canvas.load(audit_id)

    def reload(self) -> None:
        """Explicit reload of the audit into both panes."""
        self._dashboard.reload()
        self._layout_canvas.reload()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            # Initial split ratio: 60% left / 40% right
            total_width = self.width()
            left_w = int(total_width * 0.60)
            right_w = total_width - left_w
            self._splitter.setSizes([left_w, right_w])
