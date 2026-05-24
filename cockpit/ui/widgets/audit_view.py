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
from cockpit.ui.widgets.audit_bom_panel import AuditBomPanel
from cockpit.ui.widgets.selection_coordinator import SelectionCoordinator


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
        
        self._bom_panel = AuditBomPanel(
            layout_query_service=layout_query_service,
            parent=self._splitter
        )
        
        self._splitter.addWidget(self._dashboard)
        self._splitter.addWidget(self._layout_canvas)
        self._splitter.addWidget(self._bom_panel)
        
        layout.addWidget(self._splitter)
        
        # Setup Coordinator
        self._coordinator = SelectionCoordinator(view_provider=lambda: self._dashboard._view)
        self._coordinator.register_dashboard(self._dashboard)
        self._coordinator.register_bom_panel(self._bom_panel)
        
        # Signal wiring
        self._dashboard.exit_requested.connect(self.exit_requested.emit)
        self._dashboard.error_occurred.connect(self.error_occurred.emit)
        self._layout_canvas.error_occurred.connect(self.error_occurred.emit)
        self._bom_panel.error_occurred.connect(self.error_occurred.emit)
        
        # Connect Dashboard to Coordinator
        self._dashboard.tht_body_clicked.connect(self._coordinator.on_tht_body_clicked)
        self._dashboard.tht_mpn_clicked.connect(self._coordinator.on_tht_mpn_clicked)
        self._dashboard.empty_clicked.connect(self._coordinator.on_empty_clicked)
        self._dashboard.esc_pressed.connect(self._coordinator.on_escape_pressed)
        
        # Connect BOM Panel to Coordinator
        self._bom_panel.bom_mpn_toggled.connect(self._coordinator.on_bom_mpn_toggled)
        self._bom_panel.bom_refdes_selected.connect(self._coordinator.on_bom_refdes_selected)
        self._bom_panel.empty_space_clicked.connect(self._coordinator.on_empty_clicked)
        
        # Connect Coordinator to Canvas
        self._coordinator.selection_changed.connect(self._layout_canvas.set_selection)
        
        self._first_show = True

    def load(self, audit_id: int) -> None:
        """Load the audit identified by audit_id into all panes."""
        self._coordinator.on_audit_loaded()
        self._dashboard.load(audit_id)
        self._bom_panel.load(audit_id)
        self._layout_canvas.load(audit_id)

    def reload(self) -> None:
        """Explicit reload of the audit into all panes."""
        self._dashboard.reload()
        # the dashboard handles saving/loading view state. 
        # For layout_canvas and bom_panel, we also need to reload.
        if self._dashboard._current_audit_id is not None:
            self._bom_panel.load(self._dashboard._current_audit_id)
        self._layout_canvas.reload()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            # Initial split ratio: 40% dashboard / 40% canvas / 20% BOM
            total_width = self.width()
            dash_w = int(total_width * 0.40)
            bom_w = int(total_width * 0.20)
            canvas_w = total_width - dash_w - bom_w
            self._splitter.setSizes([dash_w, canvas_w, bom_w])
