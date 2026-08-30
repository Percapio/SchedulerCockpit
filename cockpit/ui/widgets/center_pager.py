from enum import Enum
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QStackedWidget

from cockpit.ui.canvas.layout_canvas import LayoutCanvas, PdfSource
from cockpit.ui.widgets.checklist_view import ChecklistView
from cockpit.ui.widgets.audit_session import AuditSession
from cockpit.ui.theme import Theme

class CenterPage(Enum):
    PRIMARY_PDF = "primary"
    SECONDARY_PDF = "secondary"
    BUILD_NOTES = "notes"

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
        
    def set_segments(self, has_secondary: bool) -> None:
        # Clear existing buttons
        from cockpit.ui.widgets.qt_lifecycle import purge_widget_subtree, _drain_layout_widgets
        for widget in _drain_layout_widgets(self.layout):
            purge_widget_subtree(widget)
        self._buttons.clear()
        
        segments = [(CenterPage.PRIMARY_PDF, "Primary")]
        if has_secondary:
            segments.append((CenterPage.SECONDARY_PDF, "Reference"))
        segments.append((CenterPage.BUILD_NOTES, "Build Notes"))
        
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

class BuildNotesPane(ChecklistView):
    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(theme, parent)
        self.setMinimumHeight(theme.checklist_min_height_px())
        
    def bind(self, session: AuditSession) -> None:
        session.rows_replaced.connect(self._on_rows_replaced)
        session.row_updated.connect(self.update_row)
        session.row_reverted.connect(self.revert_row)
        self.toggle_requested.connect(session.set_verification)
        
    def _on_rows_replaced(self, view) -> None:
        self.populate_section(view.notes_rows, f"Build Notes ({len(view.notes_rows)} items)")

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

    def _on_secondary_availability(self, available: bool) -> None:
        self._selector.set_segments(available)
        self._selector.show_page(CenterPage.PRIMARY_PDF)
        
    def _on_page_changed(self, page: CenterPage) -> None:
        if page == CenterPage.BUILD_NOTES:
            self._stacked.setCurrentWidget(self._notes_pane)
        else:
            self._stacked.setCurrentWidget(self._canvas)
            source = PdfSource.PRIMARY if page == CenterPage.PRIMARY_PDF else PdfSource.SECONDARY
            self._canvas.show_source(source)

    def bind(self, session: AuditSession) -> None:
        self._session = session
        self._notes_pane.bind(session)

    def load(self, audit_id: int) -> None:
        self._selector.set_segments(has_secondary=False)
        self._canvas.load(audit_id)
        
    def unload(self) -> None:
        self._selector.set_segments(has_secondary=False)
        self._selector.show_page(CenterPage.PRIMARY_PDF)
        self._stacked.setCurrentWidget(self._canvas)
        self._canvas.unload()
        self._notes_pane.unload()

    @property
    def notes_pane(self) -> BuildNotesPane:
        return self._notes_pane
