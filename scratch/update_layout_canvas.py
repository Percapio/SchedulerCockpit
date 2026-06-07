import re
import pathlib

path = pathlib.Path(r"d:\Dev\Scheduler\Cockpit\cockpit\ui\canvas\layout_canvas.py")
content = path.read_text(encoding="utf-8")

# 1. Imports
content = content.replace(
    "from cockpit.services.views import SelectionIntent, ResolvedSelection, SelectionKind, ResolutionKind, HighlightCoord",
    "from cockpit.services.views import SelectionIntent, ResolvedSelection, SelectionKind, ResolutionKind, HighlightCoord, LayoutContext, PendingPdf\nfrom cockpit.ui.workers.render_worker import RenderJob, RenderResult, RenderFailure"
)

# 2. Signals
content = content.replace(
    "    empty_clicked = pyqtSignal()\n\n    def __init__(",
    "    empty_clicked = pyqtSignal()\n    request_render = pyqtSignal(object)\n    generation_bumped = pyqtSignal(int)\n\n    def __init__("
)

# 3. __init__ setup
init_patch = """        self._worker_alive = False
        self._pending_pdf = None
        self._render_epoch = 0

        layout = QVBoxLayout(self)"""
content = content.replace(
    "        self._render_epoch = 0\n\n        layout = QVBoxLayout(self)",
    init_patch
)

spinner_patch = """        from cockpit.ui.widgets.empty_canvas import EmptyCanvasPlaceholder
        self._empty_placeholder = EmptyCanvasPlaceholder("No assembly drawing attached")
        self._error_placeholder = EmptyCanvasPlaceholder("")
        self._spinner_placeholder = EmptyCanvasPlaceholder("Loading assembly drawing...")

        self._stacked.addWidget(self._canvas_container)
        self._stacked.addWidget(self._empty_placeholder)
        self._stacked.addWidget(self._error_placeholder)
        self._stacked.addWidget(self._spinner_placeholder)"""

content = content.replace(
    "        from cockpit.ui.widgets.empty_canvas import EmptyCanvasPlaceholder\n        self._empty_placeholder = EmptyCanvasPlaceholder(\"No assembly drawing attached\")\n        self._error_placeholder = EmptyCanvasPlaceholder(\"\")\n        \n        self._stacked.addWidget(self._canvas_container)\n        self._stacked.addWidget(self._empty_placeholder)\n        self._stacked.addWidget(self._error_placeholder)",
    spinner_patch
)

# 4. Helpers and new methods
helpers = """
    def current_render_height(self) -> int:
        return int(self._theme.canvas_zoom_render_multiplier() * self._graphics_view.viewport().height())

    def _build_context(self, pending: PendingPdf, dims: tuple[tuple[float,float], ...]) -> LayoutContext:
        return LayoutContext(
            audit_id=self._current_audit_id,
            pdf_source_file_id=pending.source_file_id,
            pdf_path=pending.path,
            page_count=len(dims),
            page_dimensions=dims
        )

    def _submit_render(self, job: RenderJob) -> None:
        if not getattr(self, "_worker_alive", False):
            return
        self.request_render.emit(job)

    def _show_spinner(self) -> None:
        self._stacked.setCurrentWidget(self._spinner_placeholder)

    def _hide_spinner(self) -> None:
        if self._stacked.currentWidget() == self._spinner_placeholder:
            self._stacked.setCurrentWidget(self._canvas_container)

    def load(self, audit_id: int) -> None:
"""

# Find the start of load()
load_idx = content.find("    def load(self, audit_id: int) -> None:")
content = content[:load_idx] + helpers.strip() + "\n" + content[load_idx+42:]

load_body = """
        self._pixmap_cache.clear()
        self._last_intent = None
        self._last_resolved = None
        self._apply_selection(clear=True)
        self._coord_cache.clear()
        
        try:
            coords = self._layout_query_service.list_pdf_coords_for_audit(audit_id)
            for c in coords:
                self._coord_cache.setdefault(c.page_index, []).append(c)
        except Exception:
            logger.exception('Exception caught in layout_canvas coords')
            
        self._current_audit_id = audit_id
        pending = self._layout_query_service.resolve_pdf_ref(audit_id)
        
        if pending is None:
            self._current_context = LayoutContext(audit_id, None, None, 0, ())
            self._current_page_index = None
            self._pending_pdf = None
            self._page_switcher.hide()
            self._empty_placeholder.set_text("No assembly drawing attached")
            self._stacked.setCurrentWidget(self._empty_placeholder)
            return

        self._pending_pdf = pending
        self._render_epoch += 1
        gen = self._render_epoch
        self.generation_bumped.emit(gen)
        
        self._show_spinner()
        self._submit_render(RenderJob(gen, pending.path, (0,), self.current_render_height(), True))

    def _on_render_ready(self, result: RenderResult) -> None:
        if result.generation != self._render_epoch:
            return
        if result.page_dimensions is None and self._current_context is None:
            return
            
        if result.target_pixel_height != self.current_render_height():
            if self._current_context is None:
                self.load(self._current_audit_id)
            else:
                self._on_resize_debounced()
            return
            
        if result.page_dimensions is not None:
            self._current_context = self._build_context(self._pending_pdf, result.page_dimensions)
            self._page_switcher.set_page_count(self._current_context.page_count)
            self._current_page_index = 0
            
        for ri in result.images:
            pixmap = QPixmap.fromImage(ri.image)
            self._pixmap_cache[ri.page_index] = (result.target_pixel_height, pixmap)
            
        if self._current_page_index in [ri.page_index for ri in result.images]:
            self._hide_spinner()
            self._stacked.setCurrentWidget(self._canvas_container)
            
            pixmap = self._pixmap_cache[self._current_page_index][1]
            self._base_pixmap_item.setPixmap(QPixmap())
            self._base_pixmap_item.setPixmap(pixmap)
            self._base_pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            
            self._scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())
            self._dim_item.setRect(self._scene.sceneRect())
            self._graphics_view.fitInView(self._base_pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
            self._current_scale = 1.0
            self._update_pan_cursor()
            
            self._apply_selection()
            
            others = tuple(i for i in range(self._current_context.page_count)
                           if i != self._current_page_index
                           and (i not in self._pixmap_cache or self._pixmap_cache[i][0] != self.current_render_height()))
            if others:
                self._submit_render(RenderJob(self._render_epoch, self._pending_pdf.path, others, self.current_render_height(), False))

    def _on_render_error(self, failure: RenderFailure) -> None:
        if failure.generation != self._render_epoch:
            return
            
        if self._current_page_index in failure.page_indices:
            self._hide_spinner()
            self._error_placeholder.set_text(f"Could not load assembly drawing: {failure.payload.summary}")
            self._stacked.setCurrentWidget(self._error_placeholder)
            self.error_occurred.emit(failure.payload)
        else:
            logger.warning("background prefetch render failed for pages %s", failure.page_indices)

    def _on_page_changed(self, page_index: int) -> None:
        self._current_page_index = page_index
        cached = self._pixmap_cache.get(page_index)
        
        if cached is not None and cached[0] == self.current_render_height():
            pixmap = cached[1]
            self._base_pixmap_item.setPixmap(QPixmap())
            self._base_pixmap_item.setPixmap(pixmap)
            self._scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())
            self._dim_item.setRect(self._scene.sceneRect())
            self._graphics_view.fitInView(self._base_pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
            self._current_scale = 1.0
            self._update_pan_cursor()
            self._apply_selection()
        else:
            self._render_epoch += 1
            gen = self._render_epoch
            self.generation_bumped.emit(gen)
            self._show_spinner()
            if self._pending_pdf:
                self._submit_render(RenderJob(gen, self._pending_pdf.path, (page_index,), self.current_render_height(), False))

    def _on_resize_debounced(self) -> None:
        if self._pending_pdf is None or self._stacked.currentWidget() != self._canvas_container:
            return
        self._pixmap_cache.clear()
        self._render_epoch += 1
        gen = self._render_epoch
        self.generation_bumped.emit(gen)
        self._show_spinner()
        self._submit_render(RenderJob(gen, self._pending_pdf.path, (self._current_page_index,), self.current_render_height(), False))

    def resizeEvent(self, event: QResizeEvent) -> None:
"""

# Replace from `load_body` to `resizeEvent`
# First, find the next method after the old _on_resize_debounced
old_methods_regex = re.compile(r"    def _render_current_page\(self\) -> None:.*?    def resizeEvent\(self, event: QResizeEvent\) -> None:", re.DOTALL)
content = old_methods_regex.sub(load_body.strip() + "\n\n    def resizeEvent(self, event: QResizeEvent) -> None:", content)

path.write_text(content, encoding="utf-8")
print("Done")
