from unittest.mock import Mock, MagicMock
import pytest
import pathlib
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPixmap

from cockpit.ui.canvas.layout_canvas import LayoutCanvas
from cockpit.ui.theme import Theme
from cockpit.services.views import LayoutContext, HighlightCoord, ResolvedSelection, ResolutionKind
from cockpit.services.layout_query import LayoutQueryService
from cockpit.layout.renderer import PdfRenderer, RenderedPage


@pytest.fixture
def theme_m3():
    return Theme.for_testing(canvas={
        "colour": {
            "highlight_pen": {"rgb": "#FF00FF"},
            "dim_overlay": {"rgb": "#000000", "alpha": 128},
            "hint_label_background": {"rgb": "#FFFFFF"},
            "hint_label_text": {"rgb": "#000000"},
            "hint_label_border": {"rgb": "#000000"}
        },
        "zoom": {"min_scale": 1.0, "max_scale": 8.0, "step": 1.25, "render_multiplier": 3.0},
        "pen_width": {"highlight_pen": 3},
        "z_order": {"base_pixmap": 0.0, "dim": 1.0, "highlight": 2.0},
        "scalar": {"highlight_scale": 2.0},
        "hint_label": {"padding_px": 4, "border_width_px": 1}
    })


@pytest.fixture
def dummy_png_bytes():
    # Valid 1x1 transparent PNG
    return b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'


@pytest.fixture
def canvas_m3(qtbot, theme_m3, dummy_png_bytes):
    layout_query_service = Mock(spec=LayoutQueryService)
    pdf_renderer = Mock(spec=PdfRenderer)
    
    # Mock render_page_png to return a RenderedPage matching requested height
    def mock_render(pdf_path, page_index, target_pixel_height):
        # preserve aspect ratio of 1:1 for simplicity
        return RenderedPage(
            png_bytes=dummy_png_bytes,
            pixel_width=target_pixel_height,
            pixel_height=target_pixel_height,
            page_index=page_index
        )
    pdf_renderer.render_page_png.side_effect = mock_render
    
    widget = LayoutCanvas(layout_query_service, pdf_renderer, theme=theme_m3)
    qtbot.addWidget(widget)
    
    widget._graphics_view.resize(800, 600) # set viewport size
    
    widget._current_page_index = 0
    widget._current_context = LayoutContext(
        audit_id=1, pdf_source_file_id=2, pdf_path=pathlib.Path("fake.pdf"), page_count=1, page_dimensions=((1000.0, 1000.0),)
    )
    
    return widget


def test_LayoutCanvas_RenderCurrentPage_CallsPdfRendererWithTargetPixelHeightTimesMultiplier(canvas_m3):
    target_h = canvas_m3._graphics_view.viewport().height()
    expected_m = canvas_m3._theme.canvas_zoom_render_multiplier()
    
    canvas_m3._render_current_page()
    
    canvas_m3._pdf_renderer.render_page_png.assert_called_once()
    kwargs = canvas_m3._pdf_renderer.render_page_png.call_args.kwargs
    assert kwargs["target_pixel_height"] == int(expected_m * target_h)


def test_LayoutCanvas_RenderCurrentPage_PixmapDimensionsReflectMultiplierTimesViewportHeight(canvas_m3):
    target_h = canvas_m3._graphics_view.viewport().height()
    expected_m = canvas_m3._theme.canvas_zoom_render_multiplier()
    
    canvas_m3._render_current_page()
    
    # Our mock makes pixel_width and pixel_height equal to target_pixel_height
    expected_h = int(expected_m * target_h)
    assert canvas_m3._scene.sceneRect().height() == expected_h


def test_LayoutCanvas_RenderCurrentPage_BasePixmapItemTransformationModeIsSmooth(canvas_m3):
    canvas_m3._render_current_page()
    
    assert canvas_m3._base_pixmap_item.transformationMode() == Qt.TransformationMode.SmoothTransformation


def test_LayoutCanvas_RenderCurrentPage_SceneRectMatchesRenderedPixmapDimensions(canvas_m3):
    canvas_m3._render_current_page()
    
    # From the mock, width == height == m * target_h
    expected_h = int(canvas_m3._theme.canvas_zoom_render_multiplier() * canvas_m3._graphics_view.viewport().height())
    assert canvas_m3._scene.sceneRect() == QRectF(0, 0, expected_h, expected_h)


def test_LayoutCanvas_RenderCurrentPage_DimItemRectMatchesSceneRect(canvas_m3):
    canvas_m3._render_current_page()
    
    assert canvas_m3._dim_item.rect() == canvas_m3._scene.sceneRect()


def test_LayoutCanvas_RenderCurrentPage_FitInViewCalledWithBasePixmapItemArgument(canvas_m3):
    canvas_m3._graphics_view.fitInView = MagicMock()
    canvas_m3._render_current_page()
    
    canvas_m3._graphics_view.fitInView.assert_called_once()
    args = canvas_m3._graphics_view.fitInView.call_args.args
    assert args[0] is canvas_m3._base_pixmap_item


def test_LayoutCanvas_RenderCurrentPage_CurrentScaleStillResetsToOne(canvas_m3):
    canvas_m3._current_scale = 3.14
    canvas_m3._render_current_page()
    
    assert canvas_m3._current_scale == 1.0


def test_LayoutCanvas_RenderCurrentPage_HighlightCoordsStillMapToCorrectSceneRegion(canvas_m3):
    canvas_m3._render_current_page()
    
    # Simulate a single refdes selection at 0.5, 0.5 (normalized) -> 500, 500 (absolute in 1000x1000 page)
    # The selection size is 0.1x0.1 in absolute, meaning 0.0001 normalized
    canvas_m3._last_resolved = ResolvedSelection(
        kind=ResolutionKind.SINGLE_REFDES,
        mpn="R123",
        mpn_set=None,
        ref_des_list=("R1",),
        coords=(HighlightCoord("R1", 0, 450.0, 450.0, 550.0, 550.0),) # center at 500, 500
    )
    canvas_m3._apply_selection()
    
    item = canvas_m3._highlight_items[0]
    # Center of scene should be expected_h / 2
    expected_h = int(canvas_m3._theme.canvas_zoom_render_multiplier() * canvas_m3._graphics_view.viewport().height())
    center = item.rect.center()
    
    assert center.x() == expected_h / 2.0
    assert center.y() == expected_h / 2.0


def test_LayoutCanvas_RenderCurrentPage_DoesNotImportFitz():
    import ast
    
    path = pathlib.Path(__file__).parent.parent.parent.parent / "cockpit" / "ui" / "canvas" / "layout_canvas.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                assert name.name != "fitz"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "fitz"
