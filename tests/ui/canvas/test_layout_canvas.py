from unittest.mock import Mock, MagicMock
import pytest
from PyQt6.QtWidgets import QApplication

from cockpit.ui.canvas.layout_canvas import LayoutCanvas, HighlightItem
from cockpit.ui.theme import Theme
from cockpit.services.views import ResolvedSelection, ResolutionKind, HighlightCoord, LayoutContext
from cockpit.services.layout_query import LayoutQueryService
from cockpit.layout.renderer import PdfRenderer
import pathlib


@pytest.fixture
def theme():
    return Theme.for_testing(canvas={
        "colour": {
            "highlight_pen": {"rgb": "#FF00FF"},
            "dim_overlay": {"rgb": "#000000", "alpha": 128},
            "hint_label_background": {"rgb": "#FFFFFF"},
            "hint_label_text": {"rgb": "#000000"},
            "hint_label_border": {"rgb": "#000000"}
        },
        "zoom": {"min_scale": 1.0, "max_scale": 8.0, "step": 1.25, "render_multiplier": 2.0},
        "pen_width": {"highlight_pen": 3},
        "z_order": {"base_pixmap": 0.0, "dim": 1.0, "highlight": 2.0},
        "scalar": {"highlight_scale": 2.0},
        "hint_label": {"padding_px": 4, "border_width_px": 1}
    })


@pytest.fixture
def canvas(qtbot, theme):
    layout_query_service = Mock(spec=LayoutQueryService)
    pdf_renderer = Mock(spec=PdfRenderer)
    widget = LayoutCanvas(layout_query_service, pdf_renderer, theme=theme)
    qtbot.addWidget(widget)
    
    # Mock some internal state so _apply_selection works without full load
    widget._current_page_index = 0
    widget._current_context = LayoutContext(
        audit_id=1, pdf_source_file_id=2, pdf_path=pathlib.Path("fake.pdf"), page_count=1, page_dimensions=((1000.0, 1000.0),)
    )
    # Add a mock rect for sceneRect to avoid geometry issues
    widget._scene.setSceneRect(0, 0, 1000, 1000)
    
    # Add a dummy pixmap so has_displayed_page() is True
    from PyQt6.QtGui import QPixmap
    dummy_px = QPixmap(10, 10)
    widget._base_pixmap_item.setPixmap(dummy_px)
    
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


def test_zoom_forward_increases_scale(canvas, qtbot):
    from PyQt6.QtGui import QWheelEvent, QMouseEvent
    from PyQt6.QtCore import QPointF, QPoint, Qt
    
    assert canvas._current_scale == 1.0
    
    event = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 120), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.ScrollBegin, False)
    canvas.wheelEvent(event)
    
    assert canvas._current_scale == 1.25
    assert canvas._graphics_view.dragMode() == canvas._graphics_view.DragMode.ScrollHandDrag

def test_zoom_backward_at_min_scale_ignored(canvas, qtbot):
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtCore import QPointF, QPoint, Qt
    
    assert canvas._current_scale == 1.0
    
    event = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, -120), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.ScrollBegin, False)
    canvas.wheelEvent(event)
    
    assert canvas._current_scale == 1.0
    assert canvas._graphics_view.dragMode() == canvas._graphics_view.DragMode.NoDrag

def test_double_click_resets_zoom(canvas, qtbot):
    from PyQt6.QtGui import QWheelEvent, QMouseEvent
    from PyQt6.QtCore import QPointF, QPoint, Qt
    
    # Zoom in
    event = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 120), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.ScrollBegin, False)
    canvas.wheelEvent(event)
    assert canvas._current_scale == 1.25
    
    # Double click
    dc_event = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick, QPointF(100, 100), QPointF(100, 100), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    canvas.mouseDoubleClickEvent(dc_event)
    
    assert canvas._current_scale == 1.0
    assert canvas._graphics_view.dragMode() == canvas._graphics_view.DragMode.NoDrag

def test_reference_mode_disables_selection_and_clicks(canvas, qtbot):
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QPointF, Qt
    from cockpit.services.views import SelectionIntent, SelectionKind, ResolutionKind, ResolvedSelection, HighlightCoord

    canvas._active_source = "secondary"
    canvas._current_context = LayoutContext(
        audit_id=1, pdf_source_file_id=2, pdf_path=pathlib.Path("fake.pdf"), page_count=1, page_dimensions=((1000.0, 1000.0),), is_reference=True
    )

    resolved = ResolvedSelection(
        kind=ResolutionKind.SINGLE_REFDES,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1",),
        coords=(HighlightCoord("R1", 0, 0.1, 0.1, 0.2, 0.2),)
    )
    canvas._layout_query_service.resolve_selection.return_value = resolved

    # set_selection should resolve and store intent/resolved silently without visual highlights in reference mode
    intent = SelectionIntent("MPN1", SelectionKind.BOM_MPN)
    canvas.set_selection(intent)
    assert canvas._last_intent == intent
    assert canvas._last_resolved == resolved
    assert not canvas._dim_item.isVisible()
    assert all(not item.isVisible() for item in canvas._highlight_items)
    assert canvas._hint_label.isHidden()

    # Mouse clicks should be ignored in reference mode
    clicked = False
    canvas.refdes_clicked.connect(lambda x: setattr(canvas, "_clicked", True))
    canvas.empty_clicked.connect(lambda: setattr(canvas, "_clicked", True))
    
    event = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(10, 10), QPointF(10, 10), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    canvas._on_graphics_view_mouse_press(event)
    assert not getattr(canvas, "_clicked", False)

def test_show_source_primary_when_active_submits_nothing(canvas, qtbot):
    from cockpit.ui.canvas.layout_canvas import PdfSource
    
    canvas._current_audit_id = 1
    canvas._active_source = PdfSource.PRIMARY
    
    requested = []
    bumped = []
    canvas.request_render.connect(requested.append)
    canvas.generation_bumped.connect(bumped.append)
    
    canvas.show_source(PdfSource.PRIMARY)
    
    assert not requested
    assert not bumped

def test_show_source_secondary_when_missing_is_noop(canvas, qtbot):
    from cockpit.ui.canvas.layout_canvas import PdfSource
    
    canvas._current_audit_id = 1
    canvas._active_source = PdfSource.PRIMARY
    canvas._secondary_pending = None
    
    requested = []
    bumped = []
    canvas.request_render.connect(requested.append)
    canvas.generation_bumped.connect(bumped.append)
    
    canvas.show_source(PdfSource.SECONDARY)
    
    assert not requested
    assert not bumped
    assert canvas.has_secondary_pdf() is False
    assert canvas._active_source == PdfSource.PRIMARY


def test_selection_in_reference_mode_applies_on_switch_to_primary(canvas, qtbot):
    from cockpit.services.views import SelectionIntent, SelectionKind, ResolutionKind, ResolvedSelection, HighlightCoord, PendingPdf

    canvas._current_audit_id = 1
    canvas._primary_pending = PendingPdf(1, pathlib.Path("primary.pdf"))
    canvas._secondary_pending = PendingPdf(2, pathlib.Path("secondary.pdf"))
    
    # Start in secondary mode
    canvas._active_source = "secondary"
    canvas._current_context = LayoutContext(
        audit_id=1, pdf_source_file_id=2, pdf_path=pathlib.Path("secondary.pdf"), page_count=1, page_dimensions=((1000.0, 1000.0),), is_reference=True
    )

    resolved = ResolvedSelection(
        kind=ResolutionKind.SINGLE_REFDES,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1",),
        coords=(HighlightCoord("R1", 0, 0.1, 0.1, 0.2, 0.2),)
    )
    canvas._layout_query_service.resolve_selection.return_value = resolved

    intent = SelectionIntent("MPN1", SelectionKind.BOM_MPN)
    canvas.set_selection(intent)
    
    # In secondary mode, highlights are hidden
    assert not canvas._dim_item.isVisible()
    assert all(not item.isVisible() for item in canvas._highlight_items)

    # Now switch back to primary mode
    canvas._active_source = "primary"
    canvas._current_context = LayoutContext(
        audit_id=1, pdf_source_file_id=1, pdf_path=pathlib.Path("primary.pdf"), page_count=1, page_dimensions=((1000.0, 1000.0),), is_reference=False
    )
    canvas._apply_selection()

    # Highlights should now appear on primary canvas
    assert canvas._dim_item.isVisible()
    assert len(canvas._highlight_items) >= 1
    assert canvas._highlight_items[0].isVisible()
