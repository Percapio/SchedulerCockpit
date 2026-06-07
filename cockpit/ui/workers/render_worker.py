import pathlib
from dataclasses import dataclass
from PyQt6.QtCore import QObject, pyqtSignal, QMutex, QMutexLocker
from PyQt6.QtGui import QImage

from cockpit.ui.error_messages import FailurePayload, render as render_error
from cockpit.layout.renderer import PdfRenderer

@dataclass(frozen=True)
class RenderJob:
    generation: int
    pdf_path: pathlib.Path
    page_indices: tuple[int, ...]
    target_pixel_height: int
    want_dimensions: bool

@dataclass(frozen=True)
class RenderedImage:
    page_index: int
    image: QImage

@dataclass(frozen=True)
class RenderResult:
    generation: int
    target_pixel_height: int
    page_dimensions: tuple[tuple[float, float], ...] | None
    images: tuple[RenderedImage, ...]

@dataclass(frozen=True)
class RenderFailure:
    generation: int
    page_indices: tuple[int, ...]
    payload: FailurePayload

class RenderWorker(QObject):
    render_ready = pyqtSignal(RenderResult)
    render_error = pyqtSignal(RenderFailure)

    def __init__(self, pdf_renderer: PdfRenderer, parent=None):
        super().__init__(parent)
        self.pdf_renderer = pdf_renderer
        self._gen_mutex = QMutex()
        self._latest_generation = 0
        self._renders_completed = 0

    def note_latest_generation(self, generation: int) -> None:
        with QMutexLocker(self._gen_mutex):
            self._latest_generation = max(self._latest_generation, generation)

    def renders_completed(self) -> int:
        with QMutexLocker(self._gen_mutex):
            return self._renders_completed

    def _on_request(self, job: RenderJob) -> None:
        with QMutexLocker(self._gen_mutex):
            if job.generation < self._latest_generation:
                return

        try:
            dims = None
            if job.want_dimensions:
                dims = self.pdf_renderer.get_page_dimensions(job.pdf_path)

            pages = self.pdf_renderer.render_pages_png(
                job.pdf_path, list(job.page_indices), job.target_pixel_height
            )

            if job.want_dimensions:
                if dims is None or len(dims) == 0:
                    payload = render_error(RuntimeError("Missing page dimensions for want_dimensions job"))
                    self.render_error.emit(RenderFailure(job.generation, job.page_indices, payload))
                    return

            images = []
            for p in pages:
                img = QImage()
                if not img.loadFromData(p.png_bytes, "PNG") or img.isNull():
                    raise RuntimeError(f"QImage decode failed for page {p.page_index}")
                images.append(RenderedImage(page_index=p.page_index, image=img))

            result = RenderResult(
                generation=job.generation,
                target_pixel_height=job.target_pixel_height,
                page_dimensions=dims,
                images=tuple(images)
            )

            with QMutexLocker(self._gen_mutex):
                self._renders_completed += 1
            
            self.render_ready.emit(result)
            
        except Exception as e:
            payload = render_error(e)
            self.render_error.emit(RenderFailure(
                generation=job.generation,
                page_indices=job.page_indices,
                payload=payload
            ))
