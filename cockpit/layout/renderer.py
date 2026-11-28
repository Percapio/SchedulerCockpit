"""PDF rendering capabilities."""

import pathlib
from dataclasses import dataclass
import fitz  # PyMuPDF

from cockpit.ingestion.errors import MalformedPdfError
from cockpit.persistence.errors import InvalidArgumentError
from cockpit.layout.pdf_access import PdfStep, pdf_section, pdf_section_if_free


@dataclass(frozen=True)
class RenderedPage:
    """A single rasterized PDF page returned by PdfRenderer.render_page_png."""
    page_index: int
    png_bytes: bytes
    pixel_width: int
    pixel_height: int


class PdfRenderer:
    """Stateless PDF renderer using PyMuPDF."""

    def __init__(self) -> None:
        pass

    def get_page_dimensions(
        self,
        path: pathlib.Path,
        *,
        allow_multipage: bool = False,
    ) -> tuple[tuple[float, float], ...]:
        """Return per-page dimensions in PDF points (1 pt = 1/72 inch)."""
        doc, page_count = _open(path, "PDF_OPEN_FAILED")
        try:
            if allow_multipage:
                if page_count < 1:
                    raise MalformedPdfError(path, "UNSUPPORTED_PAGE_COUNT", {"observed": page_count})
            else:
                if page_count not in {1, 2}:
                    # The spec dictates returning UNSUPPORTED_PAGE_COUNT if the page count is not 1 or 2
                    raise MalformedPdfError(path, "UNSUPPORTED_PAGE_COUNT", {"observed": page_count})

            dimensions = []
            with pdf_section(PdfStep.PAGE_SIZES):
                for i in range(page_count):
                    page = doc[i]
                    try:
                        dimensions.append((float(page.rect.width), float(page.rect.height)))
                    finally:
                        page = None

            return tuple(dimensions)
        finally:
            _close(doc)

    def render_page_png(
        self,
        path: pathlib.Path,
        page_index: int,
        target_pixel_height: int,
    ) -> RenderedPage:
        """Rasterize ONE page to PNG bytes at a target pixel height."""
        if target_pixel_height <= 0:
            raise InvalidArgumentError("target_pixel_height", target_pixel_height, "Must be positive")
        return self.render_pages_png(path, [page_index], target_pixel_height)[0]

    def render_pages_png(
        self,
        path: pathlib.Path,
        page_indices: list[int],
        target_pixel_height: int,
    ) -> list[RenderedPage]:
        """Rasterize multiple pages to PNG bytes at a target pixel height."""
        if target_pixel_height <= 0:
            raise InvalidArgumentError("target_pixel_height", target_pixel_height, "Must be positive")

        doc, page_count = _open(path, "PDF_OPEN_FAILED")
        try:
            results = []
            for page_index in page_indices:
                if page_index < 0 or page_index >= page_count:
                    raise InvalidArgumentError("page_index", page_index, f"Must be between 0 and {page_count - 1}")
                results.append(_raster(doc, page_index, target_pixel_height))
            return results
        except InvalidArgumentError:
            raise
        except Exception as e:
            raise MalformedPdfError(path, "PDF_RENDER_FAILED", {"cause": str(e)}) from e
        finally:
            _close(doc)


# Patch 14 §9 (INV-P1): every fitz touch below is inside a pdf_section, and every fitz reference is released
# inside the section that made it, so no MuPDF destructor runs outside the lock.

def _open(path: pathlib.Path, reason: str):
    """The open document and its page count, read in one OPEN section."""
    with pdf_section(PdfStep.OPEN):
        try:
            doc = fitz.open(str(path))
        except Exception as e:
            raise MalformedPdfError(path, reason, {"cause": str(e)}) from e
        try:
            return doc, len(doc)
        except BaseException:
            doc.close()
            doc = None
            raise


def _close(doc) -> None:
    """Closes inside a CLOSE section. close() drops the native document (Document.this) there, and a
    closed Document's wrapper holds nothing of MuPDF's, so it may be freed later, anywhere."""
    with pdf_section(PdfStep.CLOSE):
        doc.close()


def _raster(doc, page_index: int, target_pixel_height: int) -> RenderedPage:
    """One page as PNG bytes, in one PAGE_RASTER section; the page and its pixmap die inside it."""
    with pdf_section(PdfStep.PAGE_RASTER):
        page = doc[page_index]
        pix = None
        try:
            # target_pixel_height / page.rect.height is the scaling factor
            scale = target_pixel_height / page.rect.height
            mat = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            return RenderedPage(
                page_index=page_index,
                png_bytes=pix.tobytes("png"),
                pixel_width=pix.width,
                pixel_height=pix.height,
            )
        finally:
            pix = None
            page = None


def empty_store() -> bool:
    """Empties MuPDF's store if no other thread is inside PyMuPDF or waiting for it (Patch 15 §3.2, F32).

    pre:  any thread; fitz is imported only here and in parser.py (Patch 14 §9.1)
    post: true after fitz.TOOLS.store_shrink(100) ran inside pdf_section_if_free(STORE_EMPTY); false, having
          touched nothing, when the PDF lock was taken or queued for
    raises: nothing; a PyMuPDF error is logged at WARNING and returns false
    """
    import logging

    try:
        with pdf_section_if_free(PdfStep.STORE_EMPTY) as entered:
            if not entered:
                return False
            fitz.TOOLS.store_shrink(100)
            return True
    except Exception as error:
        logging.getLogger(__name__).warning("Emptying MuPDF's store failed: %s", type(error).__name__)
        return False
