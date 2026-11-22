from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QPixmap, QResizeEvent, QShowEvent, QImage
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QLabel, QGraphicsLineItem, QGraphicsItem, QGraphicsRectItem, QPushButton
)
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QWheelEvent, QMouseEvent, QTransform, QPolygonF
from PyQt6.QtCore import QRectF, QPointF

from cockpit.ui.theme import Theme

HIGHLIGHT_PEN_WIDTH = 2

class HighlightItem(QGraphicsItem):
    def __init__(self, theme: Theme):
        super().__init__()
        self._theme = theme
        self.mode = "single"
        self.rect = QRectF()
        # Phase 57 §10.3: a footprint outline; rect is then its bounding rectangle.
        self.polygon: QPolygonF | None = None
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
            if self.polygon is not None:
                painter.drawPolygon(self.polygon)
            else:
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
        self.polygon = None
        self.mode = mode

    def set_polygon(self, polygon: QPolygonF, mode: str):
        """Phase 57 §10.3: items are pooled, so set_rect clears a polygon and this sets rect to the
        polygon's bounding rectangle, on which group mode's halo and brackets sit."""
        self.prepareGeometryChange()
        self.polygon = QPolygonF(polygon)
        self.rect = self.polygon.boundingRect()
        self.mode = mode

class XrayMarkerItem(QGraphicsItem):
    """Phase 58 §6.4: one X-ray marker. Markers are their own pool, so HighlightItem keeps its z-value, and
    a test theme without the X-ray roles never meets them unless X-ray mode draws."""

    def __init__(self, theme: Theme):
        super().__init__()
        self._theme = theme
        self.rect = QRectF()
        self.polygon: QPolygonF | None = None
        self.dashed = False
        self.setZValue(self._theme.canvas_z("xray_marker"))
        self.setVisible(False)

    def boundingRect(self) -> QRectF:
        return self.rect.adjusted(-4, -4, 4, 4)

    def paint(self, painter: QPainter, option, widget: QWidget | None = None):
        pen = self._theme.canvas_pen("xray_marker_class" if self.dashed else "xray_marker")
        if self.dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.polygon is not None:
            painter.drawPolygon(self.polygon)
        else:
            painter.drawRect(self.rect)

    def set_shape(self, shape, dashed: bool) -> None:
        self.prepareGeometryChange()
        if isinstance(shape, QPolygonF):
            self.polygon = QPolygonF(shape)
            self.rect = self.polygon.boundingRect()
        else:
            self.polygon = None
            self.rect = QRectF(shape)
        self.dashed = dashed


from cockpit.persistence.errors import AuditNotFound
from cockpit.ingestion.errors import MalformedPdfError
from cockpit.services.layout_query import LayoutQueryService
from cockpit.layout.renderer import PdfRenderer
from cockpit.ui.error_messages import FailurePayload
from cockpit.ui.error_messages import render as render_error
from cockpit.services.views import SelectionIntent, ResolvedSelection, SelectionKind, ResolutionKind, HighlightCoord, LayoutContext, PendingPdf
from cockpit.ui.workers.render_worker import RenderJob, RenderResult, RenderFailure, RenderedImage
from cockpit.persistence.errors import PersistenceError
from cockpit.ui.canvas.page_switcher import PageSwitcher
from cockpit.placements.geometry import LABEL, PartHighlight, part_at
from cockpit.services.requirements import StoredLocations
from cockpit.services.xray_markers import (
    MarkerKind,
    NoScan,
    ViewFailed,
    Viewed,
    cut_legend,
    layout_markers,
)
import logging
import sqlite3
logger = logging.getLogger(__name__)

# Phase 58 §6.5: the legend's share of the view's width, and its distance from the view's edges.
LEGEND_MAX_WIDTH_FRACTION = 0.45
OVERLAY_MARGIN_PX = 12

@dataclass(frozen=True)
class RenderBudget:
    max_cached_bytes: int
    prefetch_page_limit: int

@dataclass(frozen=True)
class CachedPage:
    page_index: int
    target_pixel_height: int
    pixmap: QPixmap
    byte_size: int

class RasterPageEntries:
    def __init__(self):
        self._odict: OrderedDict[int, CachedPage] = OrderedDict()

    def __len__(self) -> int:
        return len(self._odict)

    def insert_or_replace_as_most_recent(self, page: CachedPage) -> None:
        self._odict.pop(page.page_index, None)
        self._odict[page.page_index] = page

    def touch_as_most_recent(self, page_index: int) -> None:
        if page_index in self._odict:
            self._odict.move_to_end(page_index)

    def peek(self, page_index: int) -> Optional[CachedPage]:
        """Read without altering recency order."""
        return self._odict.get(page_index)

    def least_recently_used_excluding(self, *exempt_page_indices: int) -> Optional[CachedPage]:
        for idx, page in self._odict.items():
            if idx not in exempt_page_indices:
                return page
        return None

    def remove(self, page_index: int) -> None:
        self._odict.pop(page_index, None)

    def total_bytes(self) -> int:
        return sum(p.byte_size for p in self._odict.values())

    def clear(self) -> None:
        self._odict.clear()

class RasterPageCache:
    def __init__(self, budget: RenderBudget):
        self._budget = budget
        self._entries = RasterPageEntries()

    def put(self, page: CachedPage, protected_page_index: Optional[int]) -> list[int]:
        self._entries.insert_or_replace_as_most_recent(page)
        evicted = []
        while self._entries.total_bytes() > self._budget.max_cached_bytes:
            victim = self._entries.least_recently_used_excluding(
                protected_page_index, page.page_index
            )
            if victim is None:
                logger.warning(
                    "raster cache over budget: %d bytes resident against a %d byte ceiling",
                    self._entries.total_bytes(), self._budget.max_cached_bytes
                )
                break
            self._entries.remove(victim.page_index)
            evicted.append(victim.page_index)
        return evicted

    def get(self, page_index: int, required_pixel_height: int) -> Optional[CachedPage]:
        page = self._entries.peek(page_index)
        if page is not None and page.target_pixel_height == required_pixel_height:
            self._entries.touch_as_most_recent(page_index)
            return page
        return None

    def contains_at_height(self, page_index: int, required_pixel_height: int) -> bool:
        page = self._entries.peek(page_index)
        return page is not None and page.target_pixel_height == required_pixel_height

    def resident_page_count(self) -> int:
        return len(self._entries)

    def total_bytes(self) -> int:
        return self._entries.total_bytes()

    def clear(self) -> None:
        self._entries.clear()

def measure_rendered_bytes(rendered: RenderedImage) -> int:
    """Buffer size of one rasterized page, measured on the source QImage before
    pixmap conversion so no deep copy is taken to weigh it."""
    reported = rendered.image.sizeInBytes()
    if reported > 0:
        return reported
    return rendered.image.width() * rendered.image.height() * 4

def pages_ordered_by_distance_from(current_page_index: int, page_count: int) -> list[int]:
    return sorted(
        [i for i in range(page_count) if i != current_page_index],
        key=lambda i: (abs(i - current_page_index), i)
    )

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


from enum import Enum
class PdfSource(Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"

class LayoutCanvas(QWidget):
    error_occurred = pyqtSignal(object)
    refdes_clicked = pyqtSignal(str)
    empty_clicked = pyqtSignal()
    request_render = pyqtSignal(object)
    generation_bumped = pyqtSignal(int)
    secondary_availability_changed = pyqtSignal(bool)
    # Phase 58 §6.2: true iff the primary drawing shows a page, so X-ray mode can draw.
    xray_available_changed = pyqtSignal(bool)

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
        
        self._current_audit_id: int | None = None
        self._current_context = None
        self._current_page_index: int | None = None
        self._last_intent: SelectionIntent | None = None
        self._last_resolved: ResolvedSelection | None = None
        self._current_scale = 1.0
        self._render_budget = RenderBudget(
            max_cached_bytes=self._theme.canvas_render_max_cached_bytes(),
            prefetch_page_limit=self._theme.canvas_render_prefetch_page_limit()
        )
        self._raster_cache = RasterPageCache(self._render_budget)
        # Phase 57 section 10.2: each page's located parts, for part_at.
        self._coord_cache: dict[int, list[PartHighlight]] = {}
        self._worker_alive = False
        self._pending_pdf = None
        self._primary_pending: PendingPdf | None = None
        self._secondary_pending: PendingPdf | None = None
        self._active_source: str = "primary"
        self._render_epoch = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._page_switcher = PageSwitcher(self._theme)
        self._page_switcher.page_changed.connect(self._on_page_changed)
        self._page_switcher.hide()
        # Phase 59 §3.2: a host (CenterPager's selector bar) may take the switcher into its own layout.
        self._page_switcher_released = False
        
        self._stacked = QStackedWidget()
        
        self._canvas_container = QWidget()
        canvas_layout = QVBoxLayout(self._canvas_container)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        # Patch 14 §7.3 (INV-O1): the canvas owns its scene.
        self._scene = QGraphicsScene(self)
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
        # Phase 58 §6: X-ray mode's state; the view is read when the mode first needs it.
        self._marker_items: list[XrayMarkerItem] = []
        self._marker_shapes: list[PartHighlight] = []
        self._xray_mode = False
        self._xray_reading = None
        self._xray_stale = False
        self._stale_checker = None
        self._xray_available = False
        self._legend_lines: tuple = ()

        canvas_layout.addWidget(self._graphics_view, stretch=1)
        
        self._hint_label = QLabel(self)
        self._hint_label.setProperty("class", "hint-label bold")
        self._hint_label.setTextFormat(Qt.TextFormat.PlainText)
        self._hint_label.setVisible(False)

        # Phase 58 §6.5: the X-ray legend, plain text like the hint, at the view's bottom left.
        self._legend_label = QLabel(self)
        self._legend_label.setProperty("class", "hint-label")
        self._legend_label.setTextFormat(Qt.TextFormat.PlainText)
        self._legend_label.setWordWrap(True)
        self._legend_label.setVisible(False)
        
        from cockpit.ui.widgets.empty_canvas import EmptyCanvasPlaceholder
        self._empty_placeholder = EmptyCanvasPlaceholder("No assembly drawing attached")
        self._error_placeholder = EmptyCanvasPlaceholder("")
        self._spinner_placeholder = EmptyCanvasPlaceholder("Loading assembly drawing...")

        self._stacked.addWidget(self._canvas_container)
        self._stacked.addWidget(self._empty_placeholder)
        self._stacked.addWidget(self._error_placeholder)
        self._stacked.addWidget(self._spinner_placeholder)
        
        layout.addWidget(self._page_switcher)
        layout.addWidget(self._stacked)
        
        self._resize_debouncer = QTimer(self)
        self._resize_debouncer.setSingleShot(True)
        self._resize_debouncer.setInterval(self._theme.canvas_render_resize_debounce_ms())
        self._resize_debouncer.timeout.connect(self.reissue_for_settled_viewport)

    def estimate_page_bytes(self, render_height: int) -> int:
        if not self._current_context or not self._current_context.page_dimensions:
            return 0
        max_aspect = 0.0
        for w, h in self._current_context.page_dimensions:
            if h > 0:
                max_aspect = max(max_aspect, w / h)
        return render_height * round(render_height * max_aspect) * 4

    def select_prefetch_pages(self, current_page_index: int, page_count: int, budget: RenderBudget) -> tuple[int, ...]:
        if budget.prefetch_page_limit == 0:
            return ()
        render_height = self.current_render_height()
        estimated_page_bytes = self.estimate_page_bytes(render_height)
        if estimated_page_bytes <= 0:
            # No document loaded, or no page geometry to cost a page against.
            # Prefetch is suppressed rather than admitted at zero cost.
            return ()

        candidates = pages_ordered_by_distance_from(current_page_index, page_count)
        selected: list[int] = []
        projected_bytes = self._raster_cache.total_bytes()
        for candidate in candidates:
            if len(selected) >= budget.prefetch_page_limit:
                break
            if self._raster_cache.contains_at_height(candidate, render_height):
                continue
            if projected_bytes + estimated_page_bytes > budget.max_cached_bytes:
                break
            selected.append(candidate)
            projected_bytes += estimated_page_bytes
        return tuple(selected)

    def current_render_height(self) -> int:
        return int(self._theme.canvas_zoom_render_multiplier() * self._graphics_view.viewport().height())

    def _reference_source_active(self) -> bool:
        return getattr(self, "_active_source", "primary") == "secondary"

    def _build_context(self, pending: PendingPdf, dims: tuple[tuple[float,float], ...]) -> LayoutContext:
        return LayoutContext(
            audit_id=self._current_audit_id,
            pdf_source_file_id=pending.source_file_id,
            pdf_path=pending.path,
            page_count=len(dims),
            page_dimensions=dims,
            is_reference=self._reference_source_active()
        )

    def set_render_worker_alive(self, alive: bool) -> None:
        """Declare whether a render worker is attached and accepting jobs.

        Public because MainWindow owns the worker's lifetime. It previously set
        _worker_alive by reaching two levels through AuditView, which put a
        thread-safety flag under the control of code that does not own it.
        """
        self._worker_alive = alive

    def _submit_render(self, job: RenderJob) -> None:
        if not self._worker_alive:
            return
        self.request_render.emit(job)

    def _show_spinner(self) -> None:
        self._stacked.setCurrentWidget(self._spinner_placeholder)
        self._hide_overlays()
        self._update_xray_available()

    def _hide_spinner(self) -> None:
        if self._stacked.currentWidget() == self._spinner_placeholder:
            self._stacked.setCurrentWidget(self._canvas_container)

    def has_displayed_page(self) -> bool:
        return not self._base_pixmap_item.pixmap().isNull()

    def reset_canvas_geometry(self) -> None:
        self._base_pixmap_item.setPixmap(QPixmap())
        self._dim_item.setRect(QRectF())
        self._dim_item.setVisible(False)
        self._graphics_view.setTransform(QTransform())
        self._current_scale = 1.0
        assert not self.has_displayed_page()

    def unload(self) -> None:
        """Release every audit-scoped structure and invalidate any render job in
        flight without blocking on the worker.

        Documented exception to Unloadable post-condition (d): generation_bumped
        IS emitted. It carries no audit state to a sibling pane -- it is the
        control signal that makes teardown non-blocking (invariant I1), by
        letting the existing generation guards discard a stale result on
        arrival. Bumping before releasing state is what makes that safe.
        """
        self._render_epoch += 1
        self.generation_bumped.emit(self._render_epoch)

        self._raster_cache.clear()
        self.reset_canvas_geometry()

        self.release_highlight_pool()
        self.release_marker_pool()
        self._coord_cache.clear()
        self._xray_reading = None
        self._xray_stale = False
        self._legend_lines = ()

        self._current_audit_id = None
        self._current_context = None
        self._current_page_index = None
        self._pending_pdf = None
        self._primary_pending = None
        self._secondary_pending = None
        self._last_intent = None
        self._last_resolved = None
        self._active_source = "primary"
        self._current_scale = 1.0

        self._resize_debouncer.stop()
        self._page_switcher.reset()
        self._hint_label.setVisible(False)
        self._legend_label.setVisible(False)
        self.secondary_availability_changed.emit(False)
        self._stacked.setCurrentWidget(self._empty_placeholder)
        self._update_xray_available()

    def is_loaded(self) -> bool:
        return self._current_audit_id is not None

    def release_highlight_pool(self) -> None:
        for item in self._highlight_items:
            self._scene.removeItem(item)
        self._highlight_items.clear()

    def release_marker_pool(self) -> None:
        for item in self._marker_items:
            self._scene.removeItem(item)
        self._marker_items.clear()
        self._marker_shapes = []

    def load(self, audit_id: int) -> None:
        self.unload()
        
        self._current_audit_id = audit_id
        self._primary_pending = self._layout_query_service.resolve_pdf_ref(audit_id)
        self._secondary_pending = getattr(self._layout_query_service, "resolve_secondary_pdf_ref", lambda aid: None)(audit_id)
        self._active_source = "primary"
        self.secondary_availability_changed.emit(self.has_secondary_pdf())
        if self._xray_mode:
            self._load_xray_view()
        self._render_source("primary", page_index=0)

    def _render_source(self, source: str, page_index: int) -> None:
        pending = self._primary_pending if source == "primary" else self._secondary_pending
        if pending is None:
            self._current_context = LayoutContext(self._current_audit_id, None, None, 0, (), is_reference=(source == "secondary"))
            self._current_page_index = None
            self._pending_pdf = None
            self._page_switcher.hide()
            self._empty_placeholder.set_text("No assembly drawing attached")
            self._stacked.setCurrentWidget(self._empty_placeholder)
            self._hide_overlays()
            self._update_xray_available()
            return

        self._coord_cache.clear()
        if source == "primary":
            try:
                # Phase 57 §10.2: every BOM designator's locations, placements first.
                parts = self._layout_query_service.list_clickable_parts(self._current_audit_id)
                for part in parts:
                    self._coord_cache.setdefault(part.page_index, []).append(part)
            except Exception:
                logger.exception('Exception caught in layout_canvas coords')
        self._apply_selection(clear=True)
        self._pending_pdf = pending
        self._render_epoch += 1
        gen = self._render_epoch
        self.generation_bumped.emit(gen)
        self._show_spinner()
        self._submit_render(RenderJob(gen, pending.path, (page_index,), self.current_render_height(), True, is_reference=self._reference_source_active()))

    def has_secondary_pdf(self) -> bool:
        return self._current_audit_id is not None and self._secondary_pending is not None

    def show_source(self, source: PdfSource) -> None:
        if source == PdfSource.SECONDARY and not self.has_secondary_pdf():
            logger.warning("Attempted to show secondary source when none is pending")
            return
            
        source_str = source.value
        if self._active_source == source_str:
            return
            
        self._raster_cache.clear()
        self._active_source = source_str
        self._current_page_index = 0
        self._render_source(self._active_source, page_index=0)

    def accept_render_result(self, result: RenderResult) -> None:
        if result.generation != self._render_epoch:
            return
        if result.page_dimensions is None and self._current_context is None:
            return
            
        rendered_height = result.target_pixel_height
            
        if result.page_dimensions is not None:
            self._current_page_index = 0
            self._current_context = self._build_context(self._pending_pdf, result.page_dimensions)
            self._page_switcher.set_page_count(self._current_context.page_count, is_reference=self._reference_source_active())
            
        for ri in result.images:
            pixmap = QPixmap.fromImage(ri.image)
            byte_size = measure_rendered_bytes(ri)
            page = CachedPage(
                page_index=ri.page_index,
                target_pixel_height=rendered_height,
                pixmap=pixmap,
                byte_size=byte_size
            )
            self._raster_cache.put(page, self._current_page_index)
            
        if self._current_page_index not in [ri.page_index for ri in result.images]:
            return

        cached = self._raster_cache.get(self._current_page_index, rendered_height)
        if cached is None:
            logger.error("displayed page %d was inserted at height %d and is not resident", self._current_page_index, rendered_height)
            return

        self._hide_spinner()
        self._stacked.setCurrentWidget(self._canvas_container)
        
        pixmap = cached.pixmap
        self._base_pixmap_item.setPixmap(QPixmap())
        self._base_pixmap_item.setPixmap(pixmap)
        self._base_pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        
        self._scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())
        self._dim_item.setRect(self._scene.sceneRect())
        self._graphics_view.fitInView(self._base_pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._current_scale = 1.0
        self._update_pan_cursor()

        self._apply_selection()
        self._update_xray_available()

        if rendered_height != self.current_render_height():
            self._resize_debouncer.start()
            return
            
        others = self.select_prefetch_pages(self._current_page_index, self._current_context.page_count, self._render_budget)
        if others:
            self._submit_render(RenderJob(self._render_epoch, self._pending_pdf.path, others, self.current_render_height(), False, is_reference=self._reference_source_active()))

    def accept_render_failure(self, failure: RenderFailure) -> None:
        if failure.generation != self._render_epoch:
            return
            
        if self._current_page_index in failure.page_indices:
            if not self.has_displayed_page():
                self._hide_spinner()
                self._error_placeholder.set_text(f"Could not load assembly drawing: {failure.payload.summary}")
                self._stacked.setCurrentWidget(self._error_placeholder)
                self._hide_overlays()
                self._update_xray_available()
                self.error_occurred.emit(failure.payload)
            else:
                logger.warning("render failed at height %d for pages %s", self.current_render_height(), failure.page_indices)
        else:
            logger.warning("background prefetch render failed for pages %s", failure.page_indices)

    def _on_page_changed(self, page_index: int) -> None:
        # Invariant I3: the switcher is a child widget and can deliver into an
        # unloaded canvas. reset() is contracted not to emit page_changed, but
        # a queued emission from before the reset must also terminate here.
        if self._pending_pdf is None:
            return
        self._current_page_index = page_index
        cached = self._raster_cache.get(page_index, self.current_render_height())
        
        if cached is not None:
            pixmap = cached.pixmap
            self._base_pixmap_item.setPixmap(QPixmap())
            self._base_pixmap_item.setPixmap(pixmap)
            self._scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())
            self._dim_item.setRect(self._scene.sceneRect())
            self._graphics_view.fitInView(self._base_pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
            self._current_scale = 1.0
            self._update_pan_cursor()
            self._apply_selection()
            self._update_xray_available()
        else:
            self._render_epoch += 1
            gen = self._render_epoch
            self.generation_bumped.emit(gen)
            self._show_spinner()
            if self._pending_pdf:
                self._submit_render(RenderJob(gen, self._pending_pdf.path, (page_index,), self.current_render_height(), False, is_reference=self._reference_source_active()))

    def reissue_for_settled_viewport(self) -> None:
        if self._pending_pdf is None or self._stacked.currentWidget() != self._canvas_container:
            return
        self._render_epoch += 1
        gen = self._render_epoch
        self.generation_bumped.emit(gen)
        self._submit_render(RenderJob(gen, self._pending_pdf.path, (self._current_page_index,), self.current_render_height(), False, is_reference=self._reference_source_active()))


    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        
        if self._hint_label.isVisible():
            self._position_hint_label()
        if self._legend_label.isVisible():
            self._show_legend()

        if self._current_context is not None and self._current_page_index is not None:
            if self._stacked.currentWidget() == self._canvas_container:
                self._resize_debouncer.start()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)

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
        if self._reference_source_active():
            return
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
            
            # Phase 57 §10.3: the smallest outline under the point, then the smallest label box
            # within today's 2 pt tolerance; ties in natural order.
            shown = (
                self._marker_shapes if self._xray_mode
                else self._coord_cache.get(self._current_page_index, [])
            )
            designator = part_at(self._current_page_index, pdf_x, pdf_y, shown)
            if designator is not None:
                self.refdes_clicked.emit(designator)
                return
            self.empty_clicked.emit()

    def _update_pan_cursor(self) -> None:
        if self._current_scale > 1.0:
            self._graphics_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        else:
            self._graphics_view.setDragMode(QGraphicsView.DragMode.NoDrag)

    def set_selection(self, intent: SelectionIntent) -> None:
        if intent.kind == SelectionKind.CLEAR:
            # Phase 58 §6.4: forgetting the selection repaints; in X-ray mode the markers stay.
            self._last_intent = None
            self._last_resolved = None
            self._apply_selection()
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
        if not self.has_displayed_page():
            return
        if self._current_context is None:
            return
            
        scene_rect = self._scene.sceneRect()
        pw = scene_rect.width()
        ph = scene_rect.height()
        
        page_dim = self._current_context.page_dimensions[coord.page_index]
        pdf_w, pdf_h = page_dim[0], page_dim[1]
        
        scale_x = pw / pdf_w
        scale_y = ph / pdf_h
        
        if coord.outline:
            # Phase 57 §10.3: a footprint outline, each vertex mapped as rectangles are; drawn at its
            # true size, never enlarged by highlight_scale.
            item.set_polygon(QPolygonF([QPointF(x * scale_x, y * scale_y) for x, y in coord.outline]), mode)
            item.setVisible(True)
            return
        
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

    @staticmethod
    def _missing(resolved: ResolvedSelection) -> set[str]:
        """The selection's designators with no highlight: resolve_selection's not_located."""
        located = {c.ref_des for c in resolved.coords}
        return (set(resolved.ref_des_list) - located) | (set(resolved.not_located) & set(resolved.ref_des_list))

    def _format_group_hint(self, resolved: ResolvedSelection) -> str:
        n = len(resolved.ref_des_list)
        missing = len(self._missing(resolved))
        k = n - missing
        prefix = resolved.mpn if resolved.mpn else "Selected components"
        if missing:
            return f"{prefix}: {k} of {n} footprints highlighted ({missing} missing)"
        return f"{prefix}: {n} of {n} footprints highlighted"

    @staticmethod
    def _unused_line(resolved: ResolvedSelection) -> list[str]:
        """§10.4: the last line of a hint that already shows, never a hint of its own."""
        if resolved.placements_unused_reason and not resolved.has_placements:
            return [f"Placements not used: {resolved.placements_unused_reason}"]
        return []

    def _show_hint(self, lines: list[str]) -> None:
        if not lines:
            self._hint_label.setVisible(False)
            return
        self._hint_label.setText("\n".join(lines))
        self._hint_label.adjustSize()
        self._position_hint_label()
        self._hint_label.setVisible(True)

    def _apply_selection(self, clear: bool = False) -> None:
        """Repaints the overlays: X-ray markers in X-ray mode, else the kept selection.

        Phase 58 §6.4: clear=True hides every overlay, for a source change, a reload, a placeholder or the
        spinner; forgetting a selection is set_selection(CLEAR), which repaints.
        """
        if (
            clear
            or self._reference_source_active()
            or not self.has_displayed_page()
            or self._stacked.currentWidget() is not self._canvas_container
        ):
            self._hide_overlays()
            return
        if self._xray_mode:
            self._paint_xray()
            return
        self._hide_xray_overlays()
        resolved = self._last_resolved
        if resolved is None:
            self._dim_item.setVisible(False)
            self._hide_all_highlights()
            self._hint_label.setVisible(False)
            self._page_switcher.set_other_page_indicator(False)
            return

        if resolved.kind == ResolutionKind.SINGLE_REFDES:
            # Phase 57 §10.3: every highlight on the current page; the indicator for the rest.
            on_page = [c for c in resolved.coords if c.page_index == self._current_page_index]
            self._dim_item.setVisible(True)
            self._ensure_highlight_pool(max(1, len(on_page)))
            self._hide_all_highlights()
            for i, coord in enumerate(on_page):
                self._paint_highlight_rect(self._highlight_items[i], coord, mode="single")
            from_label = resolved.has_placements and any(c.source == LABEL for c in resolved.coords)
            self._show_hint(["From its label"] if from_label else [])
            self._page_switcher.set_other_page_indicator(
                any(c.page_index != self._current_page_index for c in resolved.coords)
            )

        elif resolved.kind == ResolutionKind.MULTI_REFDES:
            self._dim_item.setVisible(True)
            self._hide_all_highlights()
            self._show_hint(
                [f"{resolved.mpn} has {len(resolved.ref_des_list)} footprints — click MPN to highlight."]
                + self._unused_line(resolved)
            )
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind in (ResolutionKind.GROUP_REFDES, ResolutionKind.MULTI_MPN_GROUP):
            self._dim_item.setVisible(True)
            self._hide_all_highlights()
            
            coords_on_page = [c for c in resolved.coords if c.page_index == self._current_page_index]
            self._ensure_highlight_pool(len(coords_on_page))
            
            for i, coord in enumerate(coords_on_page):
                self._paint_highlight_rect(self._highlight_items[i], coord, mode="group")

            # Phase 57 §10.4: no line whose count is 0; k counts designators drawn from a label.
            lines: list[str] = []
            if self._missing(resolved):
                lines.append(self._format_group_hint(resolved))
            from_labels = {c.ref_des for c in resolved.coords if c.source == LABEL}
            if from_labels and resolved.has_placements:
                lines.append(f"{len(from_labels)} of {len(resolved.ref_des_list)} from labels")
            if lines:
                lines += self._unused_line(resolved)
            self._show_hint(lines)

            has_other_page = any(c.page_index != self._current_page_index for c in resolved.coords)
            self._page_switcher.set_other_page_indicator(has_other_page)

        elif resolved.kind in (ResolutionKind.GROUP_ABSENT, ResolutionKind.MULTI_MPN_GROUP_ABSENT):
            self._dim_item.setVisible(True)
            self._hide_all_highlights()
            prefix = resolved.mpn if resolved.mpn else "Selected components"
            self._show_hint(
                [f"{prefix}: 0 of {len(resolved.ref_des_list)} footprints found on the assembly drawing."]
                + self._unused_line(resolved)
            )
            self._page_switcher.set_other_page_indicator(False)

        elif resolved.kind == ResolutionKind.ABSENT_FROM_PDF:
            self._dim_item.setVisible(True)
            self._hide_all_highlights()
            self._show_hint(
                [f"{resolved.mpn}: RefDes {resolved.ref_des_list[0]} not found on the assembly drawing."]
                + self._unused_line(resolved)
            )
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

    # --- Phase 58 §6: X-ray mode ---

    def set_stale_scan_checker(self, checker) -> None:
        """The identity bar's checker: (audit_id, stored fingerprint) -> stale. The canvas computes none."""
        self._stale_checker = checker

    @property
    def xray_mode(self) -> bool:
        return self._xray_mode

    def set_xray_mode(self, on: bool) -> None:
        on = bool(on)
        if on == self._xray_mode:
            return
        self._xray_mode = on
        if not on:
            self._hide_xray_overlays()
        elif self._xray_reading is None and self._current_audit_id is not None:
            self._load_xray_view()
        self._apply_selection()

    def reload_xray_state(self) -> None:
        """§6.8: re-reads the view; redraws only in X-ray mode with a primary page displayed."""
        if self._current_audit_id is None:
            return
        if not self._xray_mode:
            self._xray_reading = None
            return
        self._load_xray_view()
        if self._xray_available:
            self._apply_selection()

    def release_page_switcher(self) -> PageSwitcher:
        """Hands the page switcher to a host that lays it out; the canvas keeps driving it (Phase 59 §3.2).

        post: the switcher is out of the canvas's layout and stays the canvas's child until the host's
              layout takes it, so it is never a parentless widget that show() would open as a window;
              every later reset, hide, show, set_page_count and set_other_page_indicator reaches the
              same switcher; a second call returns it and changes nothing
        """
        if not self._page_switcher_released:
            self.layout().removeWidget(self._page_switcher)
            self._page_switcher_released = True
        return self._page_switcher

    @property
    def page_switcher(self) -> PageSwitcher:
        return self._page_switcher

    def xray_available(self) -> bool:
        return self._xray_available

    def _update_xray_available(self) -> None:
        available = (
            self._current_audit_id is not None
            and self._active_source == "primary"
            and self.has_displayed_page()
            and self._stacked.currentWidget() is self._canvas_container
        )
        if available != self._xray_available:
            self._xray_available = available
            self.xray_available_changed.emit(available)

    def _load_xray_view(self) -> None:
        """§6.3: reads the view; a database error degrades to ViewFailed and is reported once."""
        self._xray_reading = None
        self._xray_stale = False
        if self._current_audit_id is None:
            return
        try:
            view = self._layout_query_service.xray_view(self._current_audit_id)
        except (PersistenceError, sqlite3.Error) as exc:
            logger.warning("X-ray locations not read: %s", type(exc).__name__)
            self._xray_reading = ViewFailed(type(exc).__name__)
            self.error_occurred.emit(FailurePayload.from_exception(exc, "X-ray locations couldn't be read"))
            return
        if view is None:
            self._xray_reading = NoScan()
            return
        self._xray_reading = Viewed(view)
        if self._stale_checker is not None and isinstance(view.stored.reading, StoredLocations):
            try:
                self._xray_stale = bool(self._stale_checker(self._current_audit_id, view.stored.fingerprint))
            except Exception:
                logger.warning("The X-ray scan's staleness couldn't be checked")
                self._xray_stale = False

    def _hide_xray_overlays(self) -> None:
        for item in self._marker_items:
            item.setVisible(False)
        self._marker_shapes = []
        self._legend_label.setVisible(False)

    def _hide_overlays(self) -> None:
        """§6.4: every overlay, for a source change, a reload, a placeholder, the spinner and unload."""
        self._dim_item.setVisible(False)
        self._hide_all_highlights()
        self._hint_label.setVisible(False)
        self._page_switcher.set_other_page_indicator(False)
        self._hide_xray_overlays()

    def _ensure_marker_pool(self, target_n: int) -> None:
        while len(self._marker_items) < target_n:
            item = XrayMarkerItem(self._theme)
            self._scene.addItem(item)
            self._marker_items.append(item)

    def _paint_xray(self) -> None:
        self._dim_item.setVisible(False)
        self._hide_all_highlights()
        if self._xray_reading is None:
            self._load_xray_view()
        reading = self._xray_reading if self._xray_reading is not None else NoScan()
        layout = layout_markers(reading, self._coord_cache, self._current_page_index, self._xray_stale)
        self._marker_shapes = [marker.shape for marker in layout.markers]
        self._ensure_marker_pool(len(layout.markers))
        for item in self._marker_items:
            item.setVisible(False)
        for item, marker in zip(self._marker_items, layout.markers):
            self._paint_marker(item, marker)
        self._show_hint([layout.hint] if layout.hint else [])
        self._legend_lines = layout.legend_lines
        self._show_legend()
        self._page_switcher.set_other_page_indicator(layout.other_pages)

    def _paint_marker(self, item: XrayMarkerItem, marker) -> None:
        from cockpit.services.layout_query import highlight_coord

        coord = highlight_coord(marker.shape)
        scene_rect = self._scene.sceneRect()
        page_dim = self._current_context.page_dimensions[coord.page_index]
        scale_x = scene_rect.width() / page_dim[0]
        scale_y = scene_rect.height() / page_dim[1]
        if coord.outline:
            shape = QPolygonF([QPointF(x * scale_x, y * scale_y) for x, y in coord.outline])
        else:
            cx = (coord.x1 + coord.x2) / 2.0 * scale_x
            cy = (coord.y1 + coord.y2) / 2.0 * scale_y
            scale = self._theme.canvas_scalar("highlight_scale")
            width = (coord.x2 - coord.x1) * scale_x * scale
            height = (coord.y2 - coord.y1) * scale_y * scale
            shape = QRectF(cx - width / 2.0, cy - height / 2.0, width, height)
        item.set_shape(shape, dashed=marker.kind == MarkerKind.CLASS_ONLY)
        item.setVisible(True)

    def _show_legend(self) -> None:
        """§6.5: at the view's bottom left, no higher than just below the hint; cut to fit."""
        lines = self._legend_lines
        if not lines or not self._xray_mode:
            self._legend_label.setVisible(False)
            return
        view_rect = self._graphics_view.geometry()
        width = max(80, int(view_rect.width() * LEGEND_MAX_WIDTH_FRACTION))
        top_limit = view_rect.top() + OVERLAY_MARGIN_PX
        if self._hint_label.isVisible():
            top_limit = self._hint_label.geometry().bottom() + OVERLAY_MARGIN_PX
        max_height = max(1, view_rect.bottom() - OVERLAY_MARGIN_PX - top_limit)

        def fits(texts) -> bool:
            self._legend_label.setText("\n".join(texts))
            return self._legend_label.heightForWidth(width) <= max_height

        texts = cut_legend(lines, fits)
        self._legend_label.setText("\n".join(texts))
        height = min(self._legend_label.heightForWidth(width), max_height)
        self._legend_label.setGeometry(
            view_rect.x() + OVERLAY_MARGIN_PX, view_rect.bottom() - OVERLAY_MARGIN_PX - height, width, height,
        )
        self._legend_label.setVisible(True)
        self._legend_label.raise_()

    def _position_hint_label(self) -> None:
        view_rect = self._graphics_view.geometry()
        hint_w = self._hint_label.width()
        x = view_rect.x() + (view_rect.width() - hint_w) // 2
        y = view_rect.y() + 12
        self._hint_label.move(x, y)

    def apply_font_scale(self, percentage: int) -> None:
        self._font_scale_bar.update_display(percentage)
