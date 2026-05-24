"""PDF rendering capabilities."""

import pathlib
from dataclasses import dataclass
import fitz  # PyMuPDF

from cockpit.ingestion.errors import MalformedPdfError
from cockpit.persistence.errors import InvalidArgumentError


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
    ) -> tuple[tuple[float, float], ...]:
        """Return per-page dimensions in PDF points (1 pt = 1/72 inch)."""
        try:
            doc = fitz.open(str(path))
        except Exception as e:
            raise MalformedPdfError(path, "PDF_OPEN_FAILED", {"cause": str(e)}) from e

        try:
            page_count = len(doc)
            if page_count not in {1, 2}:
                # The spec dictates returning UNSUPPORTED_PAGE_COUNT if the page count is not 1 or 2
                raise MalformedPdfError(path, "UNSUPPORTED_PAGE_COUNT", {"observed": page_count})

            dimensions = []
            for i in range(page_count):
                page = doc[i]
                dimensions.append((float(page.rect.width), float(page.rect.height)))

            return tuple(dimensions)
        finally:
            doc.close()

    def render_page_png(
        self,
        path: pathlib.Path,
        page_index: int,
        target_pixel_height: int,
    ) -> RenderedPage:
        """Rasterize ONE page to PNG bytes at a target pixel height."""
        if target_pixel_height <= 0:
            raise InvalidArgumentError("target_pixel_height", target_pixel_height, "Must be positive")

        try:
            doc = fitz.open(str(path))
        except Exception as e:
            raise MalformedPdfError(path, "PDF_OPEN_FAILED", {"cause": str(e)}) from e

        try:
            page_count = len(doc)
            if page_index < 0 or page_index >= page_count:
                raise InvalidArgumentError("page_index", page_index, f"Must be between 0 and {page_count - 1}")

            page = doc[page_index]
            
            # target_pixel_height / page.rect.height is the scaling factor
            # PyMuPDF dpi argument or matrix can be used.
            # Using matrix:
            scale = target_pixel_height / page.rect.height
            mat = fitz.Matrix(scale, scale)
            
            pix = page.get_pixmap(matrix=mat, alpha=False)
            png_bytes = pix.tobytes("png")
            
            return RenderedPage(
                page_index=page_index,
                png_bytes=png_bytes,
                pixel_width=pix.width,
                pixel_height=pix.height
            )
        except InvalidArgumentError:
            raise
        except Exception as e:
            raise MalformedPdfError(path, "PDF_RENDER_FAILED", {"cause": str(e)}) from e
        finally:
            doc.close()
