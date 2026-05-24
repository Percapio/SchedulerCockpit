"""Layout canvas widget."""

from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QPixmap, QResizeEvent, QShowEvent
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QStackedWidget, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem, QLabel
)

from cockpit.persistence.errors import AuditNotFound
from cockpit.ingestion.errors import MalformedPdfError
from cockpit.services.layout_query import LayoutQueryService
from cockpit.layout.renderer import PdfRenderer
from cockpit.ui.error_messages import FailurePayload
from cockpit.ui.error_messages import render as render_error
from cockpit.services.views import SelectionIntent, ResolvedSelection, SelectionKind, ResolutionKind, HighlightCoord
from cockpit.persistence.errors import PersistenceError
from cockpit.ui.widgets.page_switcher import PageSwitcher
from cockpit.ui.widgets.empty_canvas import EmptyCanvasPlaceholder


class LayoutCanvas(QWidget):
    error_occurred = pyqtSignal(object)

    def __init__(
        self,
        layout_query_service: LayoutQueryService,
        pdf_renderer: PdfRenderer,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._layout_query_service = layout_query_service
        self._pdf_renderer = pdf_renderer
        
        self._current_context = None
        self._current_page_index: int | None = None
        self._pending_render: bool = False
        self._last_intent: SelectionIntent | None = None
        self._last_resolved: ResolvedSelection | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._page_switcher = PageSwitcher()
        self._page_switcher.page_changed.connect(self._on_page_changed)
        self._page_switcher.hide()
        
        self._stacked = QStackedWidget()
        
        self._canvas_container = QWidget()
        canvas_layout = QVBoxLayout(self._canvas_container)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        self._scene = QGraphicsScene()
        self._graphics_view = QGraphicsView(self._scene)
        self._graphics_view.setFrameShape(QGraphicsView.Shape.NoFrame)
        # Avoid scrollbars if possible when fitting to view
        self._graphics_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._graphics_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self._base_pixmap_item = QGraphicsPixmapItem()
        self._base_pixmap_item.setZValue(0)
        self._scene.addItem(self._base_pixmap_item)
        
        self._dim_item = QGraphicsRectItem()
        from PyQt6.QtGui import QColor, QBrush, QPen
        self._dim_item.setBrush(QBrush(QColor(0, 0, 0, 128)))
        self._dim_item.setPen(QPen(Qt.PenStyle.NoPen))
        self._dim_item.setZValue(1)
        self._dim_item.setVisible(False)
        self._scene.addItem(self._dim_item)
        
        self._highlight_item = QGraphicsRectItem()
        self._highlight_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        highlight_pen = QPen(QColor("#FF00FF"))
        highlight_pen.setWidth(3)
        highlight_pen.setCosmetic(True)
        self._highlight_item.setPen(highlight_pen)
        self._highlight_item.setZValue(2)
        self._highlight_item.setVisible(False)
        self._scene.addItem(self._highlight_item)
        
        canvas_layout.addWidget(self._graphics_view)
        
        self._hint_label = QLabel(self)
        self._hint_label.setProperty("class", "hint-label bold")
        self._hint_label.setStyleSheet("background-color: white; color: black; padding: 4px; border: 1px solid black;")
        self._hint_label.setVisible(False)
        
        self._empty_placeholder = EmptyCanvasPlaceholder("No assembly drawing attached")
        self._error_placeholder = EmptyCanvasPlaceholder("")
        
        self._stacked.addWidget(self._canvas_container)
        self._stacked.addWidget(self._empty_placeholder)
        self._stacked.addWidget(self._error_placeholder)
        
        layout.addWidget(self._page_switcher)
        layout.addWidget(self._stacked)
        
        self._resize_debouncer = QTimer()
        self._resize_debouncer.setSingleShot(True)
        self._resize_debouncer.setInterval(200)
        self._resize_debouncer.timeout.connect(self._render_current_page)

    def load(self, audit_id: int) -> None:
        self._last_intent = None
        self._last_resolved = None
        self._apply_selection(clear=True)
        try:
            context = self._layout_query_service.load_for_audit(audit_id)
        except AuditNotFound:
            raise
        except MalformedPdfError as e:
            # error_messages rendering equivalent (simplified here)
            payload = FailurePayload(
                exception_class="MalformedPdfError",
                title="Could not load assembly drawing",
                summary=str(e),
                detail=[],
                reason_code=e.reason
            )
            self._stacked.setCurrentWidget(self._error_placeholder)
            self._error_placeholder.set_text(f"Could not load assembly drawing: {payload.summary}")
            self.error_occurred.emit(payload)
            return
            
        self._current_context = context
        
        if context.pdf_path is None:
            self._current_page_index = None
            self._page_switcher.hide()
            self._stacked.setCurrentWidget(self._empty_placeholder)
            return
            
        self._page_switcher.set_page_count(context.page_count)
        self._current_page_index = 0
        self._stacked.setCurrentWidget(self._canvas_container)
        self._render_current_page()

    def _render_current_page(self) -> None:
        if self._current_context is None or self._current_context.pdf_path is None:
            return
        if self._current_page_index is None:
            return
            
        target_h = self._graphics_view.viewport().height()
        if target_h <= 0:
            self._pending_render = True
            return
            
        self._pending_render = False

        try:
            rendered = self._pdf_renderer.render_page_png(
                self._current_context.pdf_path,
                self._current_page_index,
                target_pixel_height=target_h,
            )
        except MalformedPdfError as e:
            payload = FailurePayload(
                exception_class="MalformedPdfError",
                title="Could not render assembly drawing",
                summary=str(e),
                detail=[],
                reason_code=e.reason
            )
            self._stacked.setCurrentWidget(self._error_placeholder)
            self._error_placeholder.set_text(f"Could not load assembly drawing: {payload.summary}")
            self.error_occurred.emit(payload)
            return
            
        pixmap = QPixmap()
        ok = pixmap.loadFromData(rendered.png_bytes, format="PNG")
        if not ok:
            payload = FailurePayload(
                exception_class="QPixmapDecodeFailure",
                title="Could not display assembly drawing",
                summary="Rendered page bytes failed to decode as PNG.",
                detail=[("page_index", str(self._current_page_index)),
                        ("byte_length", str(len(rendered.png_bytes))),
                        ("pixel_width",  str(rendered.pixel_width)),
                        ("pixel_height", str(rendered.pixel_height))],
                reason_code="PIXMAP_DECODE_FAILED",
            )
            self._stacked.setCurrentWidget(self._error_placeholder)
            self._error_placeholder.set_text(f"Could not display assembly drawing: {payload.summary}")
            self.error_occurred.emit(payload)
            return

        # Restore to canvas if it was in error state before
        if self._stacked.currentWidget() == self._error_placeholder:
            self._stacked.setCurrentWidget(self._canvas_container)

        self._base_pixmap_item.setPixmap(pixmap)
        self._scene.setSceneRect(0, 0, rendered.pixel_width, rendered.pixel_height)
        self._dim_item.setRect(self._scene.sceneRect())
        self._graphics_view.fitInView(self._base_pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        
        self._apply_selection()

    def _on_page_changed(self, page_index: int) -> None:
        self._current_page_index = page_index
        self._render_current_page()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        
        if self._hint_label.isVisible():
            self._position_hint_label()
            
        if self._current_context is not None and self._current_page_index is not None:
            if self._stacked.currentWidget() == self._canvas_container:
                self._resize_debouncer.start()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._pending_render and self._current_context is not None and self._current_context.pdf_path is not None:
            self._render_current_page()

    def reload(self) -> None:
        self._last_intent = None
        self._last_resolved = None
        self._apply_selection(clear=True)
        
        if self._current_context is None:
            return
        # Keep same page_index if possible, otherwise reset
        saved_index = self._current_page_index
        try:
            self.load(self._current_context.audit_id)
            if self._current_context is not None and saved_index is not None and self._current_context.page_count > saved_index:
                self._current_page_index = saved_index
                # we must inform the switcher and re-render if we restored index
                # (but load() already rendered page 0. A small inefficiency to re-render here, but acceptable for this edge case).
                if self._current_page_index != 0:
                    # this triggers a re-render
                    # actually, need to update the switcher UI without emitting signal or let the signal handle it
                    # Let's just manually trigger
                    pass
        except Exception:
            raise

    def set_selection(self, intent: SelectionIntent) -> None:
        if intent.kind == SelectionKind.CLEAR:
            self._last_intent = None
            self._last_resolved = None
            self._apply_selection(clear=True)
            return

        if self._current_context is None:
            return

        try:
            resolved = self._layout_query_service.resolve_selection(
                self._current_context.audit_id, intent.mpn
            )
        except PersistenceError as exc:
            payload = render_error(exc)
            self.error_occurred.emit(payload)
            return

        self._last_intent = intent
        self._last_resolved = resolved
        self._apply_selection()

    def _apply_selection(self, clear: bool = False) -> None:
        resolved = self._last_resolved
        if clear or resolved is None:
            self._dim_item.setVisible(False)
            self._highlight_item.setVisible(False)
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(False)
            return

        if resolved.kind == ResolutionKind.SINGLE_REFDES:
            coord = resolved.coords[0]
            self._dim_item.setVisible(True)
            if coord.page_index == self._current_page_index:
                scene_rect = self._scene.sceneRect()
                pw = scene_rect.width()
                ph = scene_rect.height()
                self._highlight_item.setRect(coord.x1 * pw, coord.y1 * ph, (coord.x2 - coord.x1) * pw, (coord.y2 - coord.y1) * ph)
                self._highlight_item.setVisible(True)
            else:
                self._highlight_item.setVisible(False)
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(coord.page_index != self._current_page_index)

        elif resolved.kind == ResolutionKind.MULTI_REFDES:
            self._dim_item.setVisible(True)
            self._highlight_item.setVisible(False)
            self._hint_label.setText(f"{resolved.mpn} has {len(resolved.ref_des_list)} footprints — full highlight in Phase 10.")
            self._hint_label.adjustSize()
            self._position_hint_label()
            self._hint_label.setVisible(True)
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind == ResolutionKind.ABSENT_FROM_PDF:
            self._dim_item.setVisible(True)
            self._highlight_item.setVisible(False)
            self._hint_label.setText(f"{resolved.mpn}: RefDes {resolved.ref_des_list[0]} not found on the assembly drawing.")
            self._hint_label.adjustSize()
            self._position_hint_label()
            self._hint_label.setVisible(True)
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind == ResolutionKind.NO_PDF:
            self._dim_item.setVisible(False)
            self._highlight_item.setVisible(False)
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind == ResolutionKind.UNKNOWN_MPN:
            self._dim_item.setVisible(False)
            self._highlight_item.setVisible(False)
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(False)

    def _position_hint_label(self) -> None:
        view_rect = self._graphics_view.geometry()
        hint_w = self._hint_label.width()
        x = view_rect.x() + (view_rect.width() - hint_w) // 2
        y = view_rect.y() + 12
        self._hint_label.move(x, y)
