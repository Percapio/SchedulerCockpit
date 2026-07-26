import pathlib
from unittest.mock import Mock, patch
import pytest
import fitz

from cockpit.layout.renderer import PdfRenderer
from cockpit.ingestion.errors import MalformedPdfError


def make_pdf(path: pathlib.Path, num_pages: int) -> pathlib.Path:
    doc = fitz.open()
    for _ in range(num_pages):
        doc.new_page(width=600.0, height=800.0)
    doc.save(str(path))
    doc.close()
    return path


def test_get_page_dimensions_primary_success(tmp_path):
    renderer = PdfRenderer()
    
    pdf_1 = make_pdf(tmp_path / "page_1.pdf", 1)
    dims_1 = renderer.get_page_dimensions(pdf_1, allow_multipage=False)
    assert len(dims_1) == 1
    assert dims_1 == ((600.0, 800.0),)

    pdf_2 = make_pdf(tmp_path / "page_2.pdf", 2)
    dims_2 = renderer.get_page_dimensions(pdf_2, allow_multipage=False)
    assert len(dims_2) == 2
    assert dims_2 == ((600.0, 800.0), (600.0, 800.0))

    # Test default argument omitted enforces {1, 2}
    dims_default = renderer.get_page_dimensions(pdf_1)
    assert len(dims_default) == 1


def test_get_page_dimensions_primary_rejects_multipage(tmp_path):
    renderer = PdfRenderer()
    pdf_3 = make_pdf(tmp_path / "page_3.pdf", 3)
    
    with pytest.raises(MalformedPdfError) as exc_info:
        renderer.get_page_dimensions(pdf_3, allow_multipage=False)
    assert exc_info.value.reason == "UNSUPPORTED_PAGE_COUNT"
    assert exc_info.value.detail == {"observed": 3}

    with pytest.raises(MalformedPdfError) as exc_info_default:
        renderer.get_page_dimensions(pdf_3)
    assert exc_info_default.value.reason == "UNSUPPORTED_PAGE_COUNT"


def test_get_page_dimensions_reference_success(tmp_path):
    renderer = PdfRenderer()
    
    pdf_3 = make_pdf(tmp_path / "ref_3.pdf", 3)
    dims_3 = renderer.get_page_dimensions(pdf_3, allow_multipage=True)
    assert len(dims_3) == 3
    assert dims_3 == ((600.0, 800.0),) * 3

    pdf_5 = make_pdf(tmp_path / "ref_5.pdf", 5)
    dims_5 = renderer.get_page_dimensions(pdf_5, allow_multipage=True)
    assert len(dims_5) == 5
    assert dims_5 == ((600.0, 800.0),) * 5


def test_get_page_dimensions_reference_rejects_empty(tmp_path):
    renderer = PdfRenderer()
    
    # Test 0-page document by mocking fitz.open returning an empty doc mock
    mock_doc = Mock()
    mock_doc.__len__ = Mock(return_value=0)
    
    with patch("fitz.open", return_value=mock_doc):
        with pytest.raises(MalformedPdfError) as exc_info:
            renderer.get_page_dimensions(pathlib.Path("fake.pdf"), allow_multipage=True)
        assert exc_info.value.reason == "UNSUPPORTED_PAGE_COUNT"
        assert exc_info.value.detail == {"observed": 0}
