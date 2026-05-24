from unittest.mock import Mock, MagicMock
import pytest
from PyQt6.QtWidgets import QApplication

from cockpit.ui.widgets.layout_canvas import LayoutCanvas
from cockpit.services.views import ResolvedSelection, ResolutionKind, HighlightCoord, LayoutContext
from cockpit.services.layout_query import LayoutQueryService
from cockpit.layout.renderer import PdfRenderer
import pathlib


@pytest.fixture
def canvas(qtbot):
    layout_query_service = Mock(spec=LayoutQueryService)
    pdf_renderer = Mock(spec=PdfRenderer)
    widget = LayoutCanvas(layout_query_service, pdf_renderer)
    qtbot.addWidget(widget)
    
    # Mock some internal state so _apply_selection works without full load
    widget._current_page_index = 0
    widget._current_context = LayoutContext(
        audit_id=1, pdf_source_file_id=2, pdf_path=pathlib.Path("fake.pdf"), page_count=1, page_dimensions=((1000.0, 1000.0),)
    )
    # Add a mock rect for sceneRect to avoid geometry issues
    widget._scene.setSceneRect(0, 0, 1000, 1000)
    
    return widget


def test_apply_selection_clear(canvas):
    canvas._last_resolved = None
    canvas._apply_selection(clear=True)
    
    assert not canvas._dim_item.isVisible()
    assert all(not item.isVisible() for item in canvas._highlight_items)
    assert canvas._hint_label.isHidden()
    assert not canvas._page_switcher._other_page_indicator_visible


def test_apply_selection_single_refdes(canvas):
    canvas._last_resolved = ResolvedSelection(
        kind=ResolutionKind.SINGLE_REFDES,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1",),
        coords=(HighlightCoord("R1", 0, 0.1, 0.1, 0.2, 0.2),)
    )
    canvas._apply_selection()
    
    assert canvas._dim_item.isVisible()
    assert len(canvas._highlight_items) >= 1
    assert canvas._highlight_items[0].isVisible()
    assert canvas._hint_label.isHidden()
    assert not canvas._page_switcher._other_page_indicator_visible


def test_apply_selection_multi_refdes(canvas):
    canvas._last_resolved = ResolvedSelection(
        kind=ResolutionKind.MULTI_REFDES,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1", "R2"),
        coords=()
    )
    canvas._apply_selection()
    
    assert canvas._dim_item.isVisible()
    assert all(not item.isVisible() for item in canvas._highlight_items)
    assert not canvas._hint_label.isHidden()
    assert not canvas._page_switcher._other_page_indicator_visible


def test_apply_selection_absent_from_pdf(canvas):
    canvas._last_resolved = ResolvedSelection(
        kind=ResolutionKind.ABSENT_FROM_PDF,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1",),
        coords=()
    )
    canvas._apply_selection()
    
    assert canvas._dim_item.isVisible()
    assert all(not item.isVisible() for item in canvas._highlight_items)
    assert not canvas._hint_label.isHidden()
    assert "not found" in canvas._hint_label.text()
    assert not canvas._page_switcher._other_page_indicator_visible


def test_apply_selection_no_pdf(canvas):
    canvas._last_resolved = ResolvedSelection(
        kind=ResolutionKind.NO_PDF,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1",),
        coords=()
    )
    canvas._apply_selection()
    
    assert not canvas._dim_item.isVisible()
    assert all(not item.isVisible() for item in canvas._highlight_items)
    assert canvas._hint_label.isHidden()
    assert not canvas._page_switcher._other_page_indicator_visible


def test_apply_selection_unknown_mpn(canvas):
    canvas._last_resolved = ResolvedSelection(
        kind=ResolutionKind.UNKNOWN_MPN,
        mpn="R123",
        mpn_set=None,
        ref_des_list=(),
        coords=()
    )
    canvas._apply_selection()
    
    assert not canvas._dim_item.isVisible()
    assert all(not item.isVisible() for item in canvas._highlight_items)
    assert canvas._hint_label.isHidden()
    assert not canvas._page_switcher._other_page_indicator_visible


def test_apply_selection_group_refdes_full(canvas):
    canvas._last_resolved = ResolvedSelection(
        kind=ResolutionKind.GROUP_REFDES,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1", "R2"),
        coords=(
            HighlightCoord("R1", 0, 0.1, 0.1, 0.2, 0.2),
            HighlightCoord("R2", 0, 0.3, 0.3, 0.4, 0.4),
        )
    )
    canvas._apply_selection()
    
    assert canvas._dim_item.isVisible()
    assert len(canvas._highlight_items) >= 2
    assert canvas._highlight_items[0].isVisible()
    assert canvas._highlight_items[1].isVisible()
    # hint label shouldn't be visible for full coverage
    assert canvas._hint_label.isHidden()
    assert not canvas._page_switcher._other_page_indicator_visible


def test_apply_selection_group_refdes_partial(canvas):
    canvas._last_resolved = ResolvedSelection(
        kind=ResolutionKind.GROUP_REFDES,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1", "R2", "R3"),
        coords=(
            HighlightCoord("R1", 0, 0.1, 0.1, 0.2, 0.2),
            HighlightCoord("R2", 0, 0.3, 0.3, 0.4, 0.4),
        )
    )
    canvas._apply_selection()
    
    assert canvas._dim_item.isVisible()
    assert len(canvas._highlight_items) >= 2
    assert canvas._highlight_items[0].isVisible()
    assert canvas._highlight_items[1].isVisible()
    # hint label should be visible for partial coverage
    assert not canvas._hint_label.isHidden()
    assert "2 of 3 footprints highlighted (1 missing)" in canvas._hint_label.text()
    assert not canvas._page_switcher._other_page_indicator_visible


def test_apply_selection_group_absent(canvas):
    canvas._last_resolved = ResolvedSelection(
        kind=ResolutionKind.GROUP_ABSENT,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1", "R2"),
        coords=()
    )
    canvas._apply_selection()
    
    assert canvas._dim_item.isVisible()
    assert all(not item.isVisible() for item in canvas._highlight_items)
    assert not canvas._hint_label.isHidden()
    assert "0 of 2 footprints found" in canvas._hint_label.text()

