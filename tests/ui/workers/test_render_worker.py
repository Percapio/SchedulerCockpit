import pathlib
from unittest.mock import Mock, patch
import pytest
from PyQt6.QtGui import QImage
from PyQt6.QtCore import QObject

from cockpit.ui.workers.render_worker import RenderWorker, RenderJob, RenderResult, RenderFailure
from cockpit.layout.renderer import PdfRenderer, RenderedPage
from cockpit.ingestion.errors import MalformedPdfError


def test_render_worker_passes_is_reference_to_get_page_dimensions():
    mock_pdf_renderer = Mock(spec=PdfRenderer)
    mock_pdf_renderer.get_page_dimensions.return_value = ((100.0, 200.0), (100.0, 200.0), (100.0, 200.0))
    
    from PyQt6.QtCore import QBuffer, QIODevice
    img = QImage(1, 1, QImage.Format.Format_RGB32)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    bytes_val = bytes(buf.data())
    
    mock_pdf_renderer.render_pages_png.return_value = [
        RenderedPage(page_index=0, png_bytes=bytes_val, pixel_width=1, pixel_height=1),
        RenderedPage(page_index=1, png_bytes=bytes_val, pixel_width=1, pixel_height=1),
        RenderedPage(page_index=2, png_bytes=bytes_val, pixel_width=1, pixel_height=1),
    ]

    worker = RenderWorker(mock_pdf_renderer)
    
    ready_results = []
    error_results = []
    worker.render_ready.connect(ready_results.append)
    worker.render_error.connect(error_results.append)

    job_ref = RenderJob(
        generation=1,
        pdf_path=pathlib.Path("ref.pdf"),
        page_indices=(0, 1, 2),
        target_pixel_height=1000,
        want_dimensions=True,
        is_reference=True,
    )
    worker.note_latest_generation(1)
    worker._on_request(job_ref)

    mock_pdf_renderer.get_page_dimensions.assert_called_once_with(
        pathlib.Path("ref.pdf"), allow_multipage=True
    )
    assert len(ready_results) == 1
    assert len(error_results) == 0
    assert len(ready_results[0].page_dimensions) == 3


def test_render_worker_primary_mode_rejection():
    mock_pdf_renderer = Mock(spec=PdfRenderer)
    mock_pdf_renderer.get_page_dimensions.side_effect = MalformedPdfError(
        pathlib.Path("prim.pdf"), "UNSUPPORTED_PAGE_COUNT", {"observed": 3}
    )

    worker = RenderWorker(mock_pdf_renderer)
    
    ready_results = []
    error_results = []
    worker.render_ready.connect(ready_results.append)
    worker.render_error.connect(error_results.append)

    job_prim = RenderJob(
        generation=1,
        pdf_path=pathlib.Path("prim.pdf"),
        page_indices=(0,),
        target_pixel_height=1000,
        want_dimensions=True,
        is_reference=False,
    )
    worker.note_latest_generation(1)
    worker._on_request(job_prim)

    mock_pdf_renderer.get_page_dimensions.assert_called_once_with(
        pathlib.Path("prim.pdf"), allow_multipage=False
    )
    assert len(ready_results) == 0
    assert len(error_results) == 1
    assert error_results[0].generation == 1
