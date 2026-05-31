"""Layout canvas widget."""

from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QPixmap, QResizeEvent, QShowEvent
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QLabel, QGraphicsLineItem, QGraphicsItem, QGraphicsRectItem
)
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QWheelEvent, QMouseEvent
from PyQt6.QtCore import QRectF, QPointF

from cockpit.ui.theme import Theme

HIGHLIGHT_PEN_WIDTH = 2

class HighlightItem(QGraphicsItem):
    def __init__(self, theme: Theme):
        super().__init__()
        self._theme = theme
        self.mode = "single"
        self.rect = QRectF()
        self.setZValue(self._theme.canvas_z("highlight"))
        self.setVisible(False)
        
    def boundingRect(self) -> QRectF:
        if self.mode == "single":
            return self.rect
        else:
            margin = max(self.rect.width(), self.rect.height()) * 1.5
            return self.rect.adjusted(-margin, -margin, margin, margin)
            
    def paint(self, painter: QPainter, option, widget: QWidget | None = None):
        if self.mode == "single":
            pen = self._theme.canvas_pen("highlight_pen")
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

from cockpit.persistence.errors import AuditNotFound
from cockpit.ingestion.errors import MalformedPdfError
from cockpit.services.layout_query import LayoutQueryService
from cockpit.layout.renderer import PdfRenderer
from cockpit.ui.error_messages import FailurePayload
from cockpit.ui.error_messages import render as render_error
from cockpit.services.views import SelectionIntent, ResolvedSelection, SelectionKind, ResolutionKind, HighlightCoord
from cockpit.persistence.errors import PersistenceError
from cockpit.ui.canvas.page_switcher import PageSwitcher
from cockpit.ui.canvas.font_scale_bar import FontScaleBar
import logging
logger = logging.getLogger(__name__)


class _InnerGraphicsView(QGraphicsView):
    def __init__(self, canvas: 'LayoutCanvas', scene: QGraphicsScene):
        super().__init__(scene)
        self._canvas = canvas
        
    def wheelEvent(self, event: QWheelEvent) -> None:
        self._canvas.wheelEvent(event)
        
    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._canvas.mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._canvas._on_graphics_view_mouse_press(event)
        super().mousePressEvent(event)


class LayoutCanvas(QWidget):
    error_occurred = pyqtSignal(object)
    font_scale_change_requested = pyqtSignal(int)
    refdes_clicked = pyqtSignal(str)
    empty_clicked = pyqtSignal()

    def __init__(
        self,
        layout_query_service: LayoutQueryService,
        pdf_renderer: PdfRenderer,
        parent: QWidget | None = None,
        *,
        theme: Theme
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._layout_query_service = layout_query_service
        self._pdf_renderer = pdf_renderer
        
        self._current_context = None
        self._current_page_index: int | None = None
        self._pending_render: bool = False
        self._last_intent: SelectionIntent | None = None
        self._last_resolved: ResolvedSelection | None = None
        self._current_scale = 1.0
        self._pixmap_cache: dict[int, tuple[int, QPixmap]] = {}
        self._coord_cache: dict[int, list[HighlightCoord]] = {}

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
        self._graphics_view = _InnerGraphicsView(self, self._scene)
        self._graphics_view.setFrameShape(QGraphicsView.Shape.NoFrame)
        # Avoid scrollbars if possible when fitting to view
        self._graphics_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._graphics_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._graphics_view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._graphics_view.setDragMode(QGraphicsView.DragMode.NoDrag)
        
        self._base_pixmap_item = QGraphicsPixmapItem()
        self._base_pixmap_item.setZValue(self._theme.canvas_z("base_pixmap"))
        self._scene.addItem(self._base_pixmap_item)
        
        self._dim_item = QGraphicsRectItem()
        self._dim_item.setBrush(self._theme.canvas_brush("dim_overlay"))
        self._dim_item.setPen(QPen(Qt.PenStyle.NoPen))
        self._dim_item.setZValue(self._theme.canvas_z("dim"))
        self._dim_item.setVisible(False)
        self._scene.addItem(self._dim_item)
        
        self._highlight_items: list[HighlightItem] = []
        
        canvas_layout.addWidget(self._graphics_view, stretch=1)
        
        self._font_scale_bar = FontScaleBar(self._canvas_container)
        self._font_scale_bar.scale_decrease_requested.connect(lambda: self.font_scale_change_requested.emit(-1))
        self._font_scale_bar.scale_increase_requested.connect(lambda: self.font_scale_change_requested.emit(1))
        
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(4, 4, 4, 4)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self._font_scale_bar)
        
        canvas_layout.addLayout(footer_layout)
        
        self._hint_label = QLabel(self)
        self._hint_label.setProperty("class", "hint-label bold")
        self._hint_label.setVisible(False)
        
        from cockpit.ui.widgets.empty_canvas import EmptyCanvasPlaceholder
        self._empty_placeholder = EmptyCanvasPlaceholder("No assembly drawing attached")
        self._error_placeholder = EmptyCanvasPlaceholder("")
        
        self._stacked.addWidget(self._canvas_container)
        self._stacked.addWidget(self._empty_placeholder)
        self._stacked.addWidget(self._error_placeholder)
        
        layout.addWidget(self._page_switcher)
        layout.addWidget(self._stacked)
        
        self._resize_debouncer = QTimer(self)
        self._resize_debouncer.setSingleShot(True)
        self._resize_debouncer.setInterval(200)
        self._resize_debouncer.timeout.connect(self._render_current_page)

    def load(self, audit_id: int) -> None:
        self._pixmap_cache.clear()
        self._last_intent = None
        self._last_resolved = None
        self._apply_selection(clear=True)
        self._coord_cache.clear()
        
        try:
            coords = self._layout_query_service.list_pdf_coords_for_audit(audit_id)
            for c in coords:
                self._coord_cache.setdefault(c.page_index, []).append(c)
                
            context = self._layout_query_service.load_for_audit(audit_id)
        except AuditNotFound:
            logger.exception('Exception caught in layout_canvas')
            raise
        except MalformedPdfError as e:
            logger.exception('Exception caught in layout_canvas')
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

        m = self._theme.canvas_zoom_render_multiplier()
        render_h = int(m * target_h)

        cached = self._pixmap_cache.get(self._current_page_index)
        if cached is not None and cached[0] == render_h:
            pixmap = cached[1]
            rendered_width = pixmap.width()
            rendered_height = pixmap.height()
        else:
            try:
                rendered = self._pdf_renderer.render_page_png(
                    self._current_context.pdf_path,
                    self._current_page_index,
                    target_pixel_height=render_h,
                )
            except MalformedPdfError as e:
                logger.exception('Exception caught in layout_canvas')
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
                
            self._pixmap_cache[self._current_page_index] = (render_h, pixmap)
            rendered_width = rendered.pixel_width
            rendered_height = rendered.pixel_height

        # Restore to canvas if it was in error state before
        if self._stacked.currentWidget() == self._error_placeholder:
            self._stacked.setCurrentWidget(self._canvas_container)

        # Explicitly release the old pixmap memory before assigning the new one
        self._base_pixmap_item.setPixmap(QPixmap())
        
        self._base_pixmap_item.setPixmap(pixmap)
        self._base_pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        
        self._scene.setSceneRect(0, 0, rendered_width, rendered_height)
        self._dim_item.setRect(self._scene.sceneRect())
        self._graphics_view.fitInView(self._base_pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._current_scale = 1.0
        self._update_pan_cursor()
        
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
            logger.exception('Exception caught in layout_canvas')
            raise

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return

        step = self._theme.canvas_zoom_step()
        factor = step if delta > 0 else 1.0 / step
        new_scale = self._current_scale * factor

        if new_scale < self._theme.canvas_zoom_min_scale():
            event.accept()
            return

        if new_scale > self._theme.canvas_zoom_max_scale():
            event.accept()
            return

        self._graphics_view.scale(factor, factor)
        self._current_scale = new_scale
        self._update_pan_cursor()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._graphics_view.resetTransform()
        if self._base_pixmap_item.pixmap() and not self._base_pixmap_item.pixmap().isNull():
            self._graphics_view.fitInView(self._base_pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._current_scale = 1.0
        self._update_pan_cursor()
        super().mouseDoubleClickEvent(event)

    def _on_graphics_view_mouse_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._current_context is None or self._current_page_index is None:
                return
            
            scene_pos = self._graphics_view.mapToScene(event.pos())
            scene_rect = self._scene.sceneRect()
            pw = scene_rect.width()
            ph = scene_rect.height()
            
            page_dim = self._current_context.page_dimensions[self._current_page_index]
            pdf_w, pdf_h = page_dim[0], page_dim[1]
            
            if pw == 0 or ph == 0:
                return
                
            scale_x = pdf_w / pw
            scale_y = pdf_h / ph
            
            pdf_x = scene_pos.x() * scale_x
            pdf_y = scene_pos.y() * scale_y
            
            page_coords = self._coord_cache.get(self._current_page_index, [])
            for c in page_coords:
                # Add a 2px logical tolerance (pdf space)
                if (c.x1 - 2) <= pdf_x <= (c.x2 + 2) and (c.y1 - 2) <= pdf_y <= (c.y2 + 2):
                    self.refdes_clicked.emit(c.ref_des)
                    return
                    
            self.empty_clicked.emit()

    def _update_pan_cursor(self) -> None:
        if self._current_scale > 1.0:
            self._graphics_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        else:
            self._graphics_view.setDragMode(QGraphicsView.DragMode.NoDrag)

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
            logger.exception('Exception caught in layout_canvas')
            payload = render_error(exc)
            self.error_occurred.emit(payload)
            return

        self._last_intent = intent
        self._last_resolved = resolved
        self._apply_selection()

    def _ensure_highlight_pool(self, target_n: int) -> None:
        while len(self._highlight_items) < target_n:
            item = HighlightItem(self._theme)
            item.setVisible(False)
            self._scene.addItem(item)
            self._highlight_items.append(item)

    def _hide_all_highlights(self) -> None:
        for item in self._highlight_items:
            item.setVisible(False)

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
        
        scale = self._theme.canvas_scalar("highlight_scale")
        new_w = orig_w * scale
        new_h = orig_h * scale
        
        item.set_rect(QRectF(cx - new_w / 2.0, cy - new_h / 2.0, new_w, new_h), mode)
        item.setVisible(True)

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

    def apply_font_scale(self, percentage: int) -> None:
        self._font_scale_bar.update_display(percentage)
