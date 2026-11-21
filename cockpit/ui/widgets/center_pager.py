from enum import Enum
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QStackedWidget

from cockpit.ui.canvas.layout_canvas import LayoutCanvas, PdfSource
from cockpit.ui.canvas.page_switcher import PageSwitcher
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
            # Phase 59 §3.4: styled by property, like the X-ray toggle beside it.
            btn.setProperty("class", "view-segment")
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

class SelectorBar(QWidget):
    """The row of view selectors at the top of the centre pane (Phase 59 §3.1).

    The side switcher is right-aligned in a left slot and the X-ray toggle left-aligned in a right
    slot; the two slots share the spare width equally, so the page strip stays on the bar's centre
    (INV-S1) whatever the switcher shows.
    """

    def __init__(
        self,
        switcher: PageSwitcher,
        strip: "SourceSelector",
        xray_toggle: QPushButton,
        gap_px: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.switcher = switcher
        self.strip = strip
        self.xray_toggle = xray_toggle

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        self.left_slot = QWidget()
        left = QHBoxLayout(self.left_slot)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        left.addStretch(1)
        left.addWidget(switcher)
        left.addSpacing(gap_px)

        self.right_slot = QWidget()
        right = QHBoxLayout(self.right_slot)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addSpacing(gap_px)
        right.addWidget(xray_toggle)
        right.addStretch(1)

        layout.addWidget(self.left_slot, stretch=1)
        layout.addWidget(strip, stretch=0)
        layout.addWidget(self.right_slot, stretch=1)


class CenterPager(QWidget):
    # Phase 58 §6.2: your click on the X-ray toggle; setting it in code emits nothing.
    xray_mode_clicked = pyqtSignal(bool)

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

        self._selector = SourceSelector()
        self._selector.page_changed.connect(self._on_page_changed)

        # Phase 58 §6.2: enabled iff the primary page shows and the canvas can draw.
        self._xray_btn = QPushButton("X-ray")
        self._xray_btn.setObjectName("xray_toggle")
        self._xray_btn.setProperty("class", "view-segment")
        self._xray_btn.setCheckable(True)
        self._xray_btn.setToolTip("Show only the audit's X-ray locations")
        self._xray_btn.setEnabled(False)
        self._xray_btn.clicked.connect(self.xray_mode_clicked.emit)
        self._current_page = CenterPage.PRIMARY_PDF

        # Phase 59 §3: the Top/Bottom switcher, the page strip and X-ray in one bar over the pages.
        self._selector_bar = SelectorBar(
            self._canvas.release_page_switcher(), self._selector, self._xray_btn,
            self._theme.selector_group_gap_px(),
        )
        self._layout.addWidget(self._selector_bar)
        self._layout.addWidget(self._stacked, stretch=1)

        # We need to listen to secondary availability changes from canvas
        self._canvas.secondary_availability_changed.connect(self._on_secondary_availability)
        self._canvas.xray_available_changed.connect(lambda _available: self._update_xray_enabled())
        
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
        self._current_page = CenterPage.PRIMARY_PDF
        self._update_xray_enabled()

    @property
    def xray_button(self) -> QPushButton:
        return self._xray_btn

    @property
    def selector_bar(self) -> SelectorBar:
        return self._selector_bar

    def set_xray_checked(self, on: bool) -> None:
        """Sets the toggle without emitting xray_mode_clicked (Phase 58 §6.2: only a click stores)."""
        self._xray_btn.setChecked(bool(on))

    def apply_selector_enablement(self, page: CenterPage) -> None:
        """Sets which selector controls take clicks for the page now showing (Phase 59 §3.2).

        post: the side switcher is enabled iff page is PRIMARY_PDF or SECONDARY_PDF; the X-ray toggle
              is enabled iff page is PRIMARY_PDF and the canvas can draw; no control's visibility or
              checked state changes
        """
        self._selector_bar.switcher.setEnabled(page in (CenterPage.PRIMARY_PDF, CenterPage.SECONDARY_PDF))
        self._xray_btn.setEnabled(page == CenterPage.PRIMARY_PDF and self._canvas.xray_available())

    def _update_xray_enabled(self) -> None:
        self.apply_selector_enablement(self._current_page)

    def _on_page_changed(self, page: CenterPage) -> None:
        self._current_page = page
        self._update_xray_enabled()
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
        self._current_page = CenterPage.PRIMARY_PDF
        self._stacked.setCurrentWidget(self._canvas)
        self._canvas.unload()
        self._notes_pane.unload()
        if self._library_segment:
            self._library_segment.unload()
        self._update_xray_enabled()

    @property
    def notes_pane(self) -> BuildNotesPane:
        return self._notes_pane

    def set_operation_in_flight(self, in_flight: bool) -> None:
        if self._library_segment:
            self._library_segment.set_operation_in_flight(in_flight)

    def is_enrichment_in_flight(self) -> bool:
        return bool(self._library_segment) and self._library_segment.is_enrichment_in_flight()

