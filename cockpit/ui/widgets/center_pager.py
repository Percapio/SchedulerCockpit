from enum import Enum
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QStackedWidget

from cockpit.ui.canvas.layout_canvas import LayoutCanvas, PdfSource
from cockpit.ui.widgets.checklist_view import ChecklistView
from cockpit.ui.widgets.build_notes_pane import BuildNotesPane
from cockpit.ui.widgets.audit_session import AuditSession
from cockpit.ui.theme import Theme

class CenterPage(Enum):
    PRIMARY_PDF = "primary"
    SECONDARY_PDF = "secondary"
    BUILD_NOTES = "notes"
    LIBRARY = "library"

class SourceSelector(QWidget):
    page_changed = pyqtSignal(object) # CenterPage

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self._buttons: dict[CenterPage, QPushButton] = {}
        self._current_page = CenterPage.PRIMARY_PDF
        
    def set_segments(self, has_secondary: bool, has_library: bool) -> None:
        # Clear existing buttons
        from cockpit.ui.widgets.qt_lifecycle import purge_widget_subtree, _drain_layout_widgets
        for widget in _drain_layout_widgets(self.layout):
            purge_widget_subtree(widget)
        self._buttons.clear()
        
        segments = [(CenterPage.PRIMARY_PDF, "Primary")]
        if has_secondary:
            segments.append((CenterPage.SECONDARY_PDF, "Reference"))
        segments.append((CenterPage.BUILD_NOTES, "Build Notes"))
        if has_library:
            segments.append((CenterPage.LIBRARY, "Library"))
        
        for page, label in segments:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setProperty("page_id", page.value)
            btn.clicked.connect(self._on_btn_clicked)
            self.layout.addWidget(btn)
            self._buttons[page] = btn
            
    def _on_btn_clicked(self) -> None:
        btn = self.sender()
        if not btn: return
        page_id = btn.property("page_id")
        page = CenterPage(page_id)
        
        if page == self._current_page:
            btn.setChecked(True) # prevent unchecking active
            return
            
        self.show_page(page)
        self.page_changed.emit(page)

    def show_page(self, page: CenterPage) -> None:
        if page not in self._buttons:
            return
        
        self._current_page = page
        for p, btn in self._buttons.items():
            btn.setChecked(p == page)

class CenterPager(QWidget):
    def __init__(
        self,
        canvas: LayoutCanvas,
        theme: Theme,
        parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._canvas = canvas
        self._theme = theme
        
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        
        self._stacked = QStackedWidget()
        self._stacked.addWidget(self._canvas)
        
        self._notes_pane = BuildNotesPane(self._theme)
        self._notes_pane.empty_space_clicked.connect(self._canvas.empty_clicked.emit)
        self._stacked.addWidget(self._notes_pane)
        self._layout.addWidget(self._stacked, stretch=1)
        
        self._footer_row = QWidget()
        footer_layout = QHBoxLayout(self._footer_row)
        footer_layout.setContentsMargins(4, 4, 4, 4)
        
        self._selector = SourceSelector()
        self._selector.page_changed.connect(self._on_page_changed)
        footer_layout.addWidget(self._selector, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self._layout.addWidget(self._footer_row)
        
        # We need to listen to secondary availability changes from canvas
        self._canvas.secondary_availability_changed.connect(self._on_secondary_availability)
        
        self._session: AuditSession | None = None
        self._library_module = None
        self._library_segment = None
        self._library_settings_controller = None
        self._has_secondary = False

    def bind_library(self, library_module, settings_controller) -> None:
        """Binds or releases the optional library module and the settings it reads.

        settings_controller is the instance injected at cockpit/ui/app.py, and
        is None exactly when library_module is None. The segment holds it as
        its only route to any library/* key.
        """
        if self._library_segment:
            self._stacked.removeWidget(self._library_segment)
            self._library_segment.deleteLater()
            self._library_segment = None
            
        if library_module is not None and settings_controller is None:
            raise ValueError("bind_library needs the settings controller alongside the module")

        self._library_module = library_module
        self._library_settings_controller = settings_controller
        if self._library_module:
            from cockpit.ui.widgets.library_segment import LibrarySegment
            self._library_segment = LibrarySegment(self._library_module, settings_controller, self)
            self._stacked.addWidget(self._library_segment)
            
        self._selector.set_segments(self._has_secondary, self._library_module is not None)

    def _on_secondary_availability(self, available: bool) -> None:
        self._has_secondary = available
        self._selector.set_segments(self._has_secondary, self._library_module is not None)
        self._selector.show_page(CenterPage.PRIMARY_PDF)
        
    def _on_page_changed(self, page: CenterPage) -> None:
        if page == CenterPage.BUILD_NOTES:
            self._stacked.setCurrentWidget(self._notes_pane)
        elif page == CenterPage.LIBRARY and self._library_segment:
            self._stacked.setCurrentWidget(self._library_segment)
        else:
            self._stacked.setCurrentWidget(self._canvas)
            source = PdfSource.PRIMARY if page == CenterPage.PRIMARY_PDF else PdfSource.SECONDARY
            self._canvas.show_source(source)
            
    def bind(self, session: AuditSession) -> None:
        self._session = session
        self._session.view_changed.connect(self._on_audit_loaded)
        
    def _on_audit_loaded(self, view) -> None:
        if view:
            self._notes_pane.load(view)
            if self._library_segment:
                # We need the bom components for the current audit
                # Actually, the view doesn't directly expose bom lines by default, let's see.
                # If we need bom lines, we can get them in load()
                pass

    def load(self, audit_id: int, bom_lines: list = None) -> None:
        self._selector.set_segments(has_secondary=False, has_library=self._library_module is not None)
        self._canvas.load(audit_id)
        if self._library_segment:
            self._library_segment.load(None, bom_lines or [])
        
    def unload(self) -> None:
        self._has_secondary = False
        self._selector.set_segments(has_secondary=False, has_library=self._library_module is not None)
        self._selector.show_page(CenterPage.PRIMARY_PDF)
        self._stacked.setCurrentWidget(self._canvas)
        self._canvas.unload()
        self._notes_pane.unload()
        if self._library_segment:
            self._library_segment.unload()

    @property
    def notes_pane(self) -> BuildNotesPane:
        return self._notes_pane

    def set_operation_in_flight(self, in_flight: bool) -> None:
        if self._library_segment:
            self._library_segment.set_operation_in_flight(in_flight)

    def is_enrichment_in_flight(self) -> bool:
        return bool(self._library_segment) and self._library_segment.is_enrichment_in_flight()

