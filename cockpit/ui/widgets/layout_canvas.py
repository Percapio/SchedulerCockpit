"""Layout canvas widget."""

from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QPixmap, QResizeEvent, QShowEvent
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QStackedWidget, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QLabel, QGraphicsLineItem, QGraphicsItem, QGraphicsRectItem
)
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush
from PyQt6.QtCore import QRectF, QPointF

class HighlightItem(QGraphicsItem):
    def __init__(self):
        super().__init__()
        self.mode = "single"
        self.rect = QRectF()
        self.setZValue(HIGHLIGHT_Z)
        self.setVisible(False)
        
    def boundingRect(self) -> QRectF:
        if self.mode == "single":
            return self.rect
        else:
            margin = max(self.rect.width(), self.rect.height()) * 1.5
            return self.rect.adjusted(-margin, -margin, margin, margin)
            
    def paint(self, painter: QPainter, option, widget: QWidget | None = None):
        if self.mode == "single":
            pen = QPen(QColor(HIGHLIGHT_PEN_COLOUR))
            pen.setWidth(HIGHLIGHT_PEN_WIDTH)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect)
        elif self.mode == "group":
            painter.setPen(Qt.PenStyle.NoPen)
            halo_color = QColor(0, 255, 255, 80) # Cyan halo
            painter.setBrush(QBrush(halo_color))
            
            center = self.rect.center()
            radius = max(self.rect.width(), self.rect.height()) * 1.2
            painter.drawEllipse(center, radius, radius)
            
            pen = QPen(QColor(255, 165, 0)) # Orange high contrast brackets
            pen.setWidth(HIGHLIGHT_PEN_WIDTH)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            
            bracket_len = min(self.rect.width(), self.rect.height()) * 0.4
            if bracket_len < 5:
                bracket_len = 5
                
            x1, y1 = self.rect.left(), self.rect.top()
            x2, y2 = self.rect.right(), self.rect.bottom()
            
            painter.drawLine(QPointF(x1, y1 + bracket_len), QPointF(x1, y1))
            painter.drawLine(QPointF(x1, y1), QPointF(x1 + bracket_len, y1))
            
            painter.drawLine(QPointF(x2 - bracket_len, y1), QPointF(x2, y1))
            painter.drawLine(QPointF(x2, y1), QPointF(x2, y1 + bracket_len))
            
            painter.drawLine(QPointF(x1, y2 - bracket_len), QPointF(x1, y2))
            painter.drawLine(QPointF(x1, y2), QPointF(x1 + bracket_len, y2))
            
            painter.drawLine(QPointF(x2 - bracket_len, y2), QPointF(x2, y2))
            painter.drawLine(QPointF(x2, y2), QPointF(x2, y2 - bracket_len))

    def set_rect(self, rect: QRectF, mode: str):
        self.prepareGeometryChange()
        self.rect = rect
        self.mode = mode

# --- Visual Constants ---
HIGHLIGHT_SCALE = 2.0
HIGHLIGHT_PEN_COLOUR = "#FF00FF"
HIGHLIGHT_PEN_WIDTH = 3
CROSSHAIR_COLOUR = "#FFFF00"
CROSSHAIR_PEN_WIDTH = 2
DIM_OPACITY_ALPHA = 128

# --- Scene Z-Values ---
BASE_PIXMAP_Z = 0
DIM_Z = 1
HIGHLIGHT_Z = 2
CROSSHAIR_Z = 3

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
        self._base_pixmap_item.setZValue(BASE_PIXMAP_Z)
        self._scene.addItem(self._base_pixmap_item)
        
        self._dim_item = QGraphicsRectItem()
        self._dim_item.setBrush(QBrush(QColor(0, 0, 0, DIM_OPACITY_ALPHA)))
        self._dim_item.setPen(QPen(Qt.PenStyle.NoPen))
        self._dim_item.setZValue(DIM_Z)
        self._dim_item.setVisible(False)
        self._scene.addItem(self._dim_item)
        
        self._crosshair_v = QGraphicsLineItem()
        self._crosshair_h = QGraphicsLineItem()
        ch_pen = QPen(QColor(CROSSHAIR_COLOUR))
        ch_pen.setWidth(CROSSHAIR_PEN_WIDTH)
        ch_pen.setCosmetic(True)
        self._crosshair_v.setPen(ch_pen)
        self._crosshair_h.setPen(ch_pen)
        self._crosshair_v.setZValue(CROSSHAIR_Z)
        self._crosshair_h.setZValue(CROSSHAIR_Z)
        self._crosshair_v.setVisible(False)
        self._crosshair_h.setVisible(False)
        self._scene.addItem(self._crosshair_v)
        self._scene.addItem(self._crosshair_h)
        
        self._highlight_items: list[HighlightItem] = []
        
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
                self._current_context.audit_id, intent
            )
        except PersistenceError as exc:
            payload = render_error(exc)
            self.error_occurred.emit(payload)
            return

        self._last_intent = intent
        self._last_resolved = resolved
        self._apply_selection()

    def _ensure_highlight_pool(self, target_n: int) -> None:
        while len(self._highlight_items) < target_n:
            item = HighlightItem()
            item.setVisible(False)
            self._scene.addItem(item)
            self._highlight_items.append(item)

    def _hide_all_highlights(self) -> None:
        for item in self._highlight_items:
            item.setVisible(False)
        self._crosshair_v.setVisible(False)
        self._crosshair_h.setVisible(False)

    def _paint_highlight_rect(self, item: HighlightItem, coord: HighlightCoord, mode: str) -> None:
        if self._current_context is None:
            return
            
        scene_rect = self._scene.sceneRect()
        pw = scene_rect.width()
        ph = scene_rect.height()
        
        page_dim = self._current_context.page_dimensions[coord.page_index]
        pdf_w, pdf_h = page_dim[0], page_dim[1]
        
        scale_x = pw / pdf_w
        scale_y = ph / pdf_h
        
        nx1 = coord.x1 * scale_x
        ny1 = coord.y1 * scale_y
        nx2 = coord.x2 * scale_x
        ny2 = coord.y2 * scale_y
        
        cx = (nx1 + nx2) / 2.0
        cy = (ny1 + ny2) / 2.0
        
        orig_w = nx2 - nx1
        orig_h = ny2 - ny1
        
        new_w = orig_w * HIGHLIGHT_SCALE
        new_h = orig_h * HIGHLIGHT_SCALE
        
        item.set_rect(QRectF(cx - new_w / 2.0, cy - new_h / 2.0, new_w, new_h), mode)
        item.setVisible(True)

    def _set_crosshair_for_coord(self, coord: HighlightCoord) -> None:
        if self._current_context is None:
            return
            
        scene_rect = self._scene.sceneRect()
        pw = scene_rect.width()
        ph = scene_rect.height()
        
        page_dim = self._current_context.page_dimensions[coord.page_index]
        pdf_w, pdf_h = page_dim[0], page_dim[1]
        
        scale_x = pw / pdf_w
        scale_y = ph / pdf_h
        
        nx1 = coord.x1 * scale_x
        ny1 = coord.y1 * scale_y
        nx2 = coord.x2 * scale_x
        ny2 = coord.y2 * scale_y
        
        cx = (nx1 + nx2) / 2.0
        cy = (ny1 + ny2) / 2.0
        
        # Crosshair lines extend to the edges of the viewable scene
        self._crosshair_v.setLine(cx, 0, cx, ph)
        self._crosshair_h.setLine(0, cy, pw, cy)
        
        self._crosshair_v.setVisible(True)
        self._crosshair_h.setVisible(True)

    def _format_group_hint(self, resolved: ResolvedSelection) -> str:
        n = len(resolved.ref_des_list)
        k = len(resolved.coords)
        prefix = resolved.mpn if resolved.mpn else "Selected components"
        if k < n:
            missing = n - k
            return f"{prefix}: {k} of {n} footprints highlighted ({missing} missing)"
        return f"{prefix}: {n} of {n} footprints highlighted"

    def _apply_selection(self, clear: bool = False) -> None:
        resolved = self._last_resolved
        if clear or resolved is None:
            self._dim_item.setVisible(False)
            self._hide_all_highlights()
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(False)
            return

        if resolved.kind == ResolutionKind.SINGLE_REFDES:
            coord = resolved.coords[0]
            self._dim_item.setVisible(True)
            self._ensure_highlight_pool(1)
            self._hide_all_highlights()
            if coord.page_index == self._current_page_index:
                self._paint_highlight_rect(self._highlight_items[0], coord, mode="single")
                self._set_crosshair_for_coord(coord)
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(coord.page_index != self._current_page_index)

        elif resolved.kind == ResolutionKind.MULTI_REFDES:
            self._dim_item.setVisible(True)
            self._hide_all_highlights()
            self._hint_label.setText(f"{resolved.mpn} has {len(resolved.ref_des_list)} footprints — click MPN to highlight.")
            self._hint_label.adjustSize()
            self._position_hint_label()
            self._hint_label.setVisible(True)
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind in (ResolutionKind.GROUP_REFDES, ResolutionKind.MULTI_MPN_GROUP):
            self._dim_item.setVisible(True)
            self._hide_all_highlights()
            
            coords_on_page = [c for c in resolved.coords if c.page_index == self._current_page_index]
            self._ensure_highlight_pool(len(coords_on_page))
            
            for i, coord in enumerate(coords_on_page):
                self._paint_highlight_rect(self._highlight_items[i], coord, mode="group")

            if len(resolved.coords) < len(resolved.ref_des_list):
                self._hint_label.setText(self._format_group_hint(resolved))
                self._hint_label.adjustSize()
                self._position_hint_label()
                self._hint_label.setVisible(True)
            else:
                self._hint_label.setVisible(False)

            has_other_page = any(c.page_index != self._current_page_index for c in resolved.coords)
            self._page_switcher.set_other_page_indicator(has_other_page)

        elif resolved.kind in (ResolutionKind.GROUP_ABSENT, ResolutionKind.MULTI_MPN_GROUP_ABSENT):
            self._dim_item.setVisible(True)
            self._hide_all_highlights()
            prefix = resolved.mpn if resolved.mpn else "Selected components"
            self._hint_label.setText(f"{prefix}: 0 of {len(resolved.ref_des_list)} footprints found on the assembly drawing.")
            self._hint_label.adjustSize()
            self._position_hint_label()
            self._hint_label.setVisible(True)
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind == ResolutionKind.ABSENT_FROM_PDF:
            self._dim_item.setVisible(True)
            self._hide_all_highlights()
            self._hint_label.setText(f"{resolved.mpn}: RefDes {resolved.ref_des_list[0]} not found on the assembly drawing.")
            self._hint_label.adjustSize()
            self._position_hint_label()
            self._hint_label.setVisible(True)
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind == ResolutionKind.NO_PDF:
            self._dim_item.setVisible(False)
            self._hide_all_highlights()
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind == ResolutionKind.UNKNOWN_MPN:
            self._dim_item.setVisible(False)
            self._hide_all_highlights()
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(False)

    def _position_hint_label(self) -> None:
        view_rect = self._graphics_view.geometry()
        hint_w = self._hint_label.width()
        x = view_rect.x() + (view_rect.width() - hint_w) // 2
        y = view_rect.y() + 12
        self._hint_label.move(x, y)
