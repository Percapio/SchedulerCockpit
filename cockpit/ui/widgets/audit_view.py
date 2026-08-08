"""Audit view container."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QSplitter

from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService
from cockpit.services.completion import CompletionService
from cockpit.ingestion.service import IngestionService
from cockpit.services.layout_query import LayoutQueryService
from cockpit.services.release import ReleaseService
from cockpit.services.setup_bom import SetupBomService
from cockpit.layout.renderer import PdfRenderer
from cockpit.ui.widgets.dashboard import Dashboard
from cockpit.ui.canvas.layout_canvas import LayoutCanvas
from cockpit.ui.widgets.audit_bom_panel import AuditBomPanel
from cockpit.ui.widgets.selection_coordinator import SelectionCoordinator
from cockpit.ui.theme import Theme

class AuditView(QWidget):
    """QSplitter container for the Dashboard and LayoutCanvas."""
    
    exit_requested = pyqtSignal()
    error_occurred = pyqtSignal(object)  # FailurePayload
    font_scale_change_requested = pyqtSignal(int)
    settings_requested = pyqtSignal()
    ops_per_board_change_requested = pyqtSignal(int, object)  # (audit_id, float | None)

    def __init__(
        self,
        checklist_service: ChecklistService,
        split_service: AuditSplitService,
        completion_service: CompletionService,
        ingestion_service: IngestionService,
        layout_query_service: LayoutQueryService,
        release_service: ReleaseService,
        setup_bom_service: SetupBomService,
        pdf_renderer: PdfRenderer,
        parent: QWidget | None = None,
        *,
        theme: Theme
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        
        from PyQt6.QtWidgets import QVBoxLayout, QLineEdit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._metadata_band = QWidget()
        self._metadata_band.setStyleSheet("margin-left: 6%;")
        self._metadata_layout = QHBoxLayout(self._metadata_band)
        self._metadata_layout.setContentsMargins(0, 0, 0, 0)
        
        header = QHBoxLayout()
        header.addWidget(self._metadata_band)
        header.addStretch(4)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search BOM & Build Notes...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setStyleSheet("margin-right: 6%;")
        self.search_input.textChanged.connect(self._on_search_changed)
        header.addWidget(self.search_input, 2)

        from PyQt6.QtWidgets import QPushButton
        self.settings_btn = QPushButton("Settings...")
        self.settings_btn.setStyleSheet("margin-right: 6%;")
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        header.addWidget(self.settings_btn)

        layout.addLayout(header)
        
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        
        self._dashboard = Dashboard(
            checklist_service=checklist_service,
            split_service=split_service,
            completion_service=completion_service,
            ingestion_service=ingestion_service,
            release_service=release_service,
            setup_bom_service=setup_bom_service,
            theme=self._theme,
            parent=self._splitter
        )
        
        self._layout_canvas = LayoutCanvas(
            layout_query_service=layout_query_service,
            pdf_renderer=pdf_renderer,
            parent=self._splitter,
            theme=self._theme
        )
        
        self._bom_panel = AuditBomPanel(
            layout_query_service=layout_query_service,
            parent=self._splitter,
            theme=self._theme
        )
        
        self._splitter.addWidget(self._dashboard)
        self._splitter.addWidget(self._layout_canvas)
        self._splitter.addWidget(self._bom_panel)
        
        self._dashboard.setMinimumWidth(self._theme.left_panel_min_width())
        
        layout.addWidget(self._splitter)
        
        # Setup Coordinator
        self._coordinator = SelectionCoordinator(
            view_provider=lambda: self._dashboard._view,
            layout_query_service=layout_query_service
        )
        self._coordinator.register_dashboard(self._dashboard)
        self._coordinator.register_bom_panel(self._bom_panel)
        
        # Signal wiring
        self._dashboard.exit_requested.connect(self.exit_requested.emit)
        self._dashboard.error_occurred.connect(self.error_occurred.emit)
        self._dashboard.metadata_changed.connect(self._on_metadata_changed)
        self._dashboard.reload_requested.connect(self.load)
        self._dashboard.ops_per_board_change_requested.connect(self.ops_per_board_change_requested.emit)
        self._layout_canvas.error_occurred.connect(self.error_occurred.emit)
        self._layout_canvas.font_scale_change_requested.connect(self.font_scale_change_requested.emit)
        self._bom_panel.error_occurred.connect(self.error_occurred.emit)
        
        # Connect Dashboard to Coordinator
        self._dashboard.tht_body_clicked.connect(self._coordinator.on_tht_body_clicked)
        self._dashboard.tht_mpn_clicked.connect(self._coordinator.on_tht_mpn_clicked)
        self._dashboard.empty_clicked.connect(self._coordinator.on_empty_clicked)
        self._dashboard.esc_pressed.connect(self._coordinator.on_escape_pressed)
        
        # Connect BOM Panel to Coordinator
        self._bom_panel.bom_row_clicked.connect(self._coordinator.on_bom_row_clicked)
        self._bom_panel.empty_space_clicked.connect(self._coordinator.on_empty_clicked)
        
        # Connect Canvas to Coordinator
        self._layout_canvas.refdes_clicked.connect(self._coordinator.on_renderer_refdes_clicked)
        self._layout_canvas.empty_clicked.connect(self._coordinator.on_empty_clicked)
        
        # Connect Coordinator to Canvas
        self._coordinator.selection_changed.connect(self._layout_canvas.set_selection)
        
        self._first_show = True
        self._bom_min_width = 200

    def unload(self) -> None:
        """Sole release point for audit-scoped state.

        Post: the Unloadable post-conditions hold for the coordinator and all
              three panes, and self is safe to show without loading.

        The coordinator is torn down first: it is the only participant holding
        references to the panes, so unloading it first guarantees no pane is
        touched by a coordinator callback after that pane has been released.

        search_input is cleared with signals blocked. QLineEdit.clear() emits
        textChanged, which routes through _on_search_changed into
        Dashboard.apply_filter() and AuditBomPanel.apply_filter() -- both of
        them panes this method has just torn down. That re-entry into a
        half-unloaded pane is exactly what post-condition (d) forbids.
        """
        self._coordinator.unload()
        self._layout_canvas.unload()
        self._bom_panel.unload()
        self._dashboard.unload()
        self._metadata_band_clear()
        was_blocked = self.search_input.blockSignals(True)
        try:
            self.search_input.clear()
        finally:
            self.search_input.blockSignals(was_blocked)

    def is_loaded(self) -> bool:
        return self._dashboard.current_audit_id() is not None

    def current_audit_id(self) -> int | None:
        return self._dashboard.current_audit_id()

    def set_render_worker_alive(self, alive: bool) -> None:
        """Owned here so MainWindow, which owns the worker's lifetime, does not
        reach two levels down into the canvas to set a thread-safety flag."""
        self._layout_canvas.set_render_worker_alive(alive)

    def load(self, audit_id: int) -> None:
        """Load the audit identified by audit_id into all panes."""
        self.unload()
        self._dashboard.setEnabled(True)
        self._layout_canvas.setEnabled(True)
        self._bom_panel.setEnabled(True)
        self._coordinator.on_audit_loaded()
        self._dashboard.load(audit_id)
        self._bom_panel.load(audit_id)
        self._layout_canvas.load(audit_id)

    def reload(self) -> None:
        """Explicit reload of the audit into all panes."""
        self._dashboard.setEnabled(True)
        self._layout_canvas.setEnabled(True)
        self._bom_panel.setEnabled(True)
        self._dashboard.reload()
        if self._dashboard.current_audit_id() is not None:
            self._bom_panel.load(self._dashboard.current_audit_id())
        self._layout_canvas.reload()
        
    def discard_if_showing(self, audit_id: int) -> bool:
        """Invalidate the view when the displayed audit was mutated or deleted underneath it."""
        if self._dashboard.current_audit_id() != audit_id:
            return False
        self.unload()
        return True
            
    def show_loading_placeholder(self) -> None:
        """Show a loading placeholder while a deferred load is pending."""
        self._dashboard.setEnabled(False)
        self._layout_canvas.setEnabled(False)
        self._bom_panel.setEnabled(False)
        # Proper styling or overlay could be added, but simple disable works as placeholder
        # and it will be re-enabled during the actual load() or reload().

    def hideEvent(self, event) -> None:
        if self.search_input.text():
            self.search_input.clear()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._apply_initial_layout)
            
    def _apply_initial_layout(self) -> None:
        screen_w = self.window().screen().size().width()
        pct = self._theme.bom_panel_min_width_percent()
        abs_min = self._theme.bom_panel_min_width_absolute()
        self._bom_min_width = max(int(screen_w * pct), abs_min)
        
        if self._has_pdf():
            self._splitter.setStretchFactor(0, 0)
            self._splitter.setStretchFactor(1, 1)
            self._splitter.setStretchFactor(2, 0)
            
            dash_w = self._dashboard.minimumWidth()
            bom_target_w = self._bom_min_width
            pcb_w = max(0, self.width() - dash_w - bom_target_w)
            self._splitter.setSizes([dash_w, pcb_w, bom_target_w])
        else:
            self._splitter.setStretchFactor(0, 2)
            self._splitter.setStretchFactor(1, 2)
            self._splitter.setStretchFactor(2, 1)
            
            # Initial split ratio: 40% dashboard / 40% canvas / 20% BOM
            total_width = self.width()
            dash_w = int(total_width * 0.40)
            bom_w = int(total_width * 0.20)
            canvas_w = max(0, total_width - dash_w - bom_w)
            self._splitter.setSizes([dash_w, canvas_w, bom_w])
            
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

    def _has_pdf(self) -> bool:
        return self._dashboard.has_pdf()

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        if index == 2:
            sizes = self._splitter.sizes()
            if sizes[2] < self._bom_min_width:
                self._splitter.splitterMoved.disconnect(self._on_splitter_moved)
                self._splitter.setSizes([sizes[0], sizes[1] + sizes[2] - self._bom_min_width, self._bom_min_width])
                self._splitter.splitterMoved.connect(self._on_splitter_moved)

    def apply_font_scale(self, percentage: int) -> None:
        self._layout_canvas.apply_font_scale(percentage)

    def flush_pending_writes(self) -> None:
        self._dashboard.flush_audit_notes()

    def _on_search_changed(self, text: str) -> None:
        query = text.strip()
        self._dashboard.apply_filter(query)
        self._bom_panel.apply_filter(query)

    def _metadata_band_clear(self) -> None:
        while self._metadata_layout.count():
            item = self._metadata_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _on_metadata_changed(self, metadata: dict) -> None:
        self._metadata_band_clear()
                
        if not metadata:
            return
            
        from PyQt6.QtWidgets import QLabel
            
        _METADATA_DISPLAY_LABELS = {
            "customer_name": "Customer",
            "sales_order_number": "S/O",
            "lead_time_days": "LT",
        }
        for key, label in _METADATA_DISPLAY_LABELS.items():
            val = metadata.get(key, "—")
            self._metadata_layout.addWidget(QLabel(f"{label}: {val}"))
            
        ac_raw = metadata.get("assembly_class")
        if ac_raw is not None:
            try:
                ac_num = int(ac_raw)
                self._metadata_layout.addWidget(QLabel(f"Class {ac_num}"))
            except (ValueError, TypeError):
                pass
            
        clean_val = metadata.get("process_clean")
        process_val: Any | None = metadata.get("process")

        if clean_val:
            lbl_process_prefix = QLabel("Process:")
            lbl_process_prefix.setStyleSheet("padding-left: 3%;")
            self._metadata_layout.addWidget(lbl_process_prefix)
            
            val_text = f"{process_val if process_val else ''} {clean_val}".strip()
            lbl_process_value = QLabel(val_text)
            lbl_process_value.setProperty("class", "hdr-process")
            self._metadata_layout.addWidget(lbl_process_value)
            
        rowc_val = metadata.get("rowc_ref")
        rowc_label = metadata.get("rowc_label")
        if rowc_val and rowc_label:
            lbl_rowc = QLabel(f"{rowc_label} {rowc_val}")
            lbl_rowc.setStyleSheet("padding-left: 3%;")
            self._metadata_layout.addWidget(lbl_rowc)
