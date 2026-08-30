"""Audit view container."""

from PyQt6.QtCore import pyqtSignal, Qt, QSettings
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QSplitter, QLineEdit
from PyQt6.QtGui import QKeySequence, QShortcut

from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService
from cockpit.services.completion import CompletionService
from cockpit.ingestion.service import IngestionService
from cockpit.services.layout_query import LayoutQueryService
from cockpit.services.release import ReleaseService
from cockpit.services.setup_bom import SetupBomService
from cockpit.layout.renderer import PdfRenderer
from cockpit.services.views import build_identity_banner

from cockpit.ui.widgets.audit_session import AuditSession
from cockpit.ui.widgets.audit_actions_bar import AuditActionsBar
from cockpit.ui.widgets.audit_identity_bar import AuditIdentityBar
from cockpit.ui.widgets.center_pager import CenterPager, CenterPage
from cockpit.ui.canvas.layout_canvas import LayoutCanvas
from cockpit.ui.widgets.audit_bom_panel import AuditBomPanel
from cockpit.ui.widgets.checklist_view import ChecklistView
from cockpit.ui.widgets.selection_coordinator import SelectionCoordinator
from cockpit.ui.widgets.chamfered_pane import ChamferedPane
from cockpit.ui.theme import Theme

class AuditView(QWidget):
    """QSplitter container for the main application panes."""
    
    # AuditActionsBar relays
    exit_requested = pyqtSignal()
    error_occurred = pyqtSignal(object)  # FailurePayload
    ops_per_board_change_requested = pyqtSignal(int, object)  # (audit_id, float | None)
    
    # Local
    settings_requested = pyqtSignal()

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
        theme: Theme,
        
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._theme = theme
        
        self._session = AuditSession(checklist_service, build_identity_banner)
        self._session.error_occurred.connect(self.error_occurred.emit)
        self._session.ops_per_board_change_requested.connect(self.ops_per_board_change_requested.emit)
        
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._esc_shortcut.activated.connect(self._on_escape_pressed)

        layout = QVBoxLayout(self)
        gap_px = self._theme.pane_gap_px()
        layout.setContentsMargins(gap_px, gap_px, gap_px, gap_px)
        
        # Header
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        
        self._identity_bar = AuditIdentityBar()
        self._identity_bar.back_requested.connect(self._on_exit_requested)
        header_layout.addWidget(self._identity_bar)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search BOM & Build Notes...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_changed)
        header_layout.addWidget(self.search_input)

        from PyQt6.QtWidgets import QPushButton
        self.settings_btn = QPushButton("Settings...")
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        header_layout.addWidget(self.settings_btn)
        
        self._actions_bar = AuditActionsBar(
            split_service, completion_service, ingestion_service,
            release_service, setup_bom_service, self
        )
        self._actions_bar.bind(self._session)
        self._actions_bar.error_occurred.connect(self.error_occurred.emit)
        self._actions_bar.reload_requested.connect(self.load)
        self._actions_bar.ops_per_board_change_requested.connect(self.ops_per_board_change_requested.emit)
        self._actions_bar.exit_requested.connect(self._on_exit_requested)
        header_layout.addWidget(self._actions_bar)
        
        layout.addLayout(header_layout)
        
        # Main Splitter
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(gap_px)
        
        # Center Pager
        self._layout_canvas = LayoutCanvas(
            layout_query_service=layout_query_service,
            pdf_renderer=pdf_renderer,
            parent=self._splitter,
            theme=self._theme
        )
        self._center_pager = CenterPager(self._layout_canvas, self._theme, self._splitter)
        self._center_pager.bind(self._session)
        self._center_pager._selector.page_changed.connect(self._on_center_page_changed)
        
        center_chamfered = ChamferedPane(
            self._center_pager, 
            self._theme.pane_chamfer_px(), 
            self._theme.pane_inset_px(), 
            self._theme.pane_fill_rgb()
        )
        self._splitter.addWidget(center_chamfered)
        
        # Right Stack
        self._right_stack = QSplitter(Qt.Orientation.Vertical)
        self._right_stack.setChildrenCollapsible(False)
        self._right_stack.setHandleWidth(gap_px)
        
        self.checklist_tht = ChecklistView(self._theme)
        self.checklist_tht.setMinimumHeight(self._theme.checklist_min_height_px())
        
        tht_chamfered = ChamferedPane(
            self.checklist_tht, 
            self._theme.pane_chamfer_px(), 
            self._theme.pane_inset_px(), 
            self._theme.pane_fill_rgb()
        )
        self._right_stack.addWidget(tht_chamfered)
        
        self._bom_panel = AuditBomPanel(
            layout_query_service=layout_query_service,
            parent=self._right_stack,
            theme=self._theme
        )
        self._bom_panel.setMinimumHeight(self._theme.checklist_min_height_px())
        
        bom_chamfered = ChamferedPane(
            self._bom_panel, 
            self._theme.pane_chamfer_px(), 
            self._theme.pane_inset_px(), 
            self._theme.pane_fill_rgb()
        )
        self._right_stack.addWidget(bom_chamfered)
        
        self._right_stack.setSizes([1, 1])
        self._splitter.addWidget(self._right_stack)
        
        layout.addWidget(self._splitter, stretch=1)
        
        # Setup Coordinator
        self._coordinator = SelectionCoordinator(
            view_provider=self._session.current_view,
            layout_query_service=layout_query_service
        )
        self._coordinator.register_tht_pane(self.checklist_tht)
        self._coordinator.register_notes_pane(self._center_pager.notes_pane)
        self._coordinator.register_bom_panel(self._bom_panel)
        
        # Signal wiring
        self._session.identity_changed.connect(self._identity_bar.set_identity)
        
        self._session.rows_replaced.connect(self._on_rows_replaced)
        
        self.checklist_tht.body_clicked.connect(self._coordinator.on_tht_body_clicked)
        self.checklist_tht.mpn_clicked.connect(self._coordinator.on_tht_mpn_clicked)
        self.checklist_tht.empty_space_clicked.connect(self._coordinator.on_empty_clicked)
        
        self._bom_panel.bom_row_clicked.connect(self._coordinator.on_bom_row_clicked)
        self._bom_panel.empty_space_clicked.connect(self._coordinator.on_empty_clicked)
        self._bom_panel.error_occurred.connect(self.error_occurred.emit)
        
        self._layout_canvas.error_occurred.connect(self.error_occurred.emit)
        self._layout_canvas.refdes_clicked.connect(self._coordinator.on_renderer_refdes_clicked)
        self._layout_canvas.empty_clicked.connect(self._coordinator.on_empty_clicked)
        
        self._coordinator.selection_changed.connect(self._layout_canvas.set_selection)
        
        self._first_show = True

    def _on_rows_replaced(self, view) -> None:
        self.checklist_tht.populate_section(view.tht_rows, f"T/H - MPN Count: {len(view.tht_rows)} | Total Placements: {view.tht_placement_count}")
        


    def _on_search_changed(self, query_text: str) -> None:
        query = query_text.strip()
        self.checklist_tht.apply_filter(query)
        self._bom_panel.apply_filter(query)
        
        if getattr(self._center_pager._selector, '_current_page', None) == CenterPage.BUILD_NOTES:
            self._center_pager.notes_pane.apply_filter(query)

    def _on_center_page_changed(self, page: CenterPage) -> None:
        if page == CenterPage.BUILD_NOTES:
            self._center_pager.notes_pane.apply_filter(self.search_input.text().strip())
        else:
            self._center_pager.notes_pane.apply_filter("")

    def _on_escape_pressed(self) -> None:
        if self.search_input.hasFocus() and self.search_input.text():
            self.search_input.clear()
        else:
            self._coordinator.on_escape_pressed()

    def _on_exit_requested(self) -> None:
        self.unload()
        self.exit_requested.emit()

    def unload(self) -> None:
        self._coordinator.unload()
        self._center_pager.unload()
        self._bom_panel.unload()
        self.checklist_tht.unload()
        self._actions_bar.unload()
        self._identity_bar.set_identity(None)
        self._session.unload()
        
        was_blocked = self.search_input.blockSignals(True)
        try:
            self.search_input.clear()
        finally:
            self.search_input.blockSignals(was_blocked)

    def is_loaded(self) -> bool:
        return self._session.current_audit_id() is not None

    def current_audit_id(self) -> int | None:
        return self._session.current_audit_id()

    def set_render_worker_alive(self, alive: bool) -> None:
        self._layout_canvas.set_render_worker_alive(alive)

    def load(self, audit_id: int) -> None:
        self.unload()
        self._coordinator.on_audit_loaded()
        self._session.load(audit_id)
        self._bom_panel.load(audit_id)
        self._center_pager.load(audit_id)

    def reload(self) -> None:
        self._session.reload()
        if self._session.current_audit_id() is not None:
            self._bom_panel.load(self._session.current_audit_id())
        self._layout_canvas.reload()
        
    def discard_if_showing(self, audit_id: int) -> bool:
        if self._session.current_audit_id() != audit_id:
            return False
        self.unload()
        return True
            

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
        right_min_w = self._theme.right_panel_min_width()
        
        if self._session.has_pdf():
            self._splitter.setStretchFactor(0, 1)
            self._splitter.setStretchFactor(1, 0)
        else:
            self._splitter.setStretchFactor(0, 2)
            self._splitter.setStretchFactor(1, 1)
            
        total_width = self.width()
        right_w = max(right_min_w, int(total_width * 0.3))
        center_w = max(0, total_width - right_w - self._splitter.handleWidth())
        self._splitter.setSizes([center_w, right_w])
        
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        if index == 1:
            right_min_w = self._theme.right_panel_min_width()
            sizes = self._splitter.sizes()
            if sizes[1] < right_min_w:
                self._splitter.splitterMoved.disconnect(self._on_splitter_moved)
                self._splitter.setSizes([sizes[0] + sizes[1] - right_min_w, right_min_w])
                self._splitter.splitterMoved.connect(self._on_splitter_moved)

    def flush_pending_writes(self) -> None:
        # no-op, previously this flushed audit notes
        pass
