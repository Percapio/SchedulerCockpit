"""Per-pane Unloadable post-conditions (Optimize06 sections 3.1-3.3, step 3 gate).

The churn smoke asserts that live counts return to a fixed set after a cycle.
That is a whole-application signal: it says teardown happened somewhere, not
that each pane discharged its own contract. These tests assert the contract
implementer by implementer, so a pane that stops releasing something is named
by the failure rather than folded into an aggregate count.

Contract under test, for every implementer:
  (a) no reference to audit-scoped data is reachable from the implementer
  (b) is_loaded() returns False
  (c) a subsequent load() yields state indistinguishable from a first load
  (d) no signal is emitted, of any kind, for any reason
  plus: safe on a never-loaded implementer, and safe to call twice.
"""

import pathlib
from unittest.mock import Mock

import pytest

from cockpit.layout.renderer import PdfRenderer
from cockpit.services.checklist import ChecklistService
from cockpit.services.completion import CompletionService
from cockpit.services.layout_query import AuditBomRowView, LayoutQueryService
from cockpit.services.split import AuditSplitService
from cockpit.services.views import (
    ActiveAuditView,
    ChecklistRowKey,
    ChecklistRowKind,
    ChecklistRowView,
    HighlightCoord,
    LayoutContext,
    SelectionIntent,
    SelectionKind,
)
from cockpit.ingestion.service import IngestionService
from cockpit.persistence.types import AuditStatus
from cockpit.ui.canvas.layout_canvas import CachedPage, LayoutCanvas
from cockpit.ui.theme import Theme
from cockpit.ui.widgets.audit_bom_panel import AuditBomPanel
from cockpit.ui.widgets.checklist_view import ChecklistView
from cockpit.ui.widgets.dashboard import Dashboard
from cockpit.ui.widgets.selection_coordinator import SelectionCoordinator

from PyQt6.QtGui import QPixmap


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def theme():
    return Theme.for_testing(
        canvas={
            "colour": {
                "highlight_pen": {"rgb": "#FF00FF"},
                "dim_overlay": {"rgb": "#000000", "alpha": 128},
                "hint_label_background": {"rgb": "#FFFFFF"},
                "hint_label_text": {"rgb": "#000000"},
                "hint_label_border": {"rgb": "#000000"},
            },
            "zoom": {"min_scale": 1.0, "max_scale": 8.0, "step": 1.25, "render_multiplier": 2.0},
            "pen_width": {"highlight_pen": 3},
            "z_order": {"base_pixmap": 0.0, "dim": 1.0, "highlight": 2.0},
            "scalar": {"highlight_scale": 2.0},
            "hint_label": {"padding_px": 4, "border_width_px": 1},
        },
        left_panel={
            "row": {
                "corner_radius_px": 4,
                "vertical_padding_px": 6,
                "horizontal_padding_px": 8,
                "gutter_px": 3,
            },
        },
        bom_panel={
            "chip": {
                "corner_radius_px": 3,
                "vertical_padding_px": 3,
                "horizontal_padding_px": 6,
                "flow_spacing_px": 4,
            },
        },
    )


def _checklist_rows(count: int) -> list[ChecklistRowView]:
    return [
        ChecklistRowView(
            key=ChecklistRowKey(kind=ChecklistRowKind.THT, item_id=i),
            primary_label=f"MPN-{i}",
            secondary_label=f"desc {i}",
            find_number=i,
            ref_des_list=(f"R{i}", f"C{i}"),
        )
        for i in range(count)
    ]


def _bom_rows(count: int) -> list[AuditBomRowView]:
    return [
        AuditBomRowView(
            find_number=i,
            component_mpn=f"MPN-{i}",
            description=f"desc {i}",
            mount_type="S",
            ref_des_list=(f"R{i}", f"C{i}"),
        )
        for i in range(count)
    ]


def _active_view(audit_id: int = 1) -> ActiveAuditView:
    return ActiveAuditView(
        audit_id=audit_id,
        part_number="PN-123",
        work_order_ref="WO-1",
        split_suffix=None,
        quantity=10,
        status=AuditStatus.NOT_CLEAR,
        split_reason=None,
        traveler_metadata={"customer_name": "TestCorp"},
        has_pdf=True,
        tht_placement_count=4,
        tht_rows=_checklist_rows(3),
        notes_rows=_checklist_rows(2),
    )


class SignalRecorder:
    """Records every emission on the signals it is attached to.

    Post-condition (d) is the one most easily violated by accident, and it is
    invisible unless something is listening.
    """

    def __init__(self):
        self.emissions: list[str] = []

    def watch(self, owner, *signal_names: str) -> None:
        for name in signal_names:
            signal = getattr(owner, name)
            signal.connect(lambda *args, _n=name: self.emissions.append(_n))


# ---------------------------------------------------------------------------
# ChecklistView
# ---------------------------------------------------------------------------

@pytest.fixture
def checklist_view(qtbot, theme):
    view = ChecklistView(theme)
    qtbot.addWidget(view)
    return view


def test_checklist_view_unload_empties_index_and_layout(checklist_view):
    checklist_view.populate_section(_checklist_rows(5), "THT")
    assert checklist_view.is_loaded()
    assert len(checklist_view._index) == 5

    checklist_view.unload()

    assert checklist_view._index == {}
    assert checklist_view._layout.count() == 0
    assert not checklist_view.is_loaded()


def test_checklist_view_unload_emits_nothing(checklist_view):
    checklist_view.populate_section(_checklist_rows(5), "THT")
    recorder = SignalRecorder()
    recorder.watch(
        checklist_view,
        "toggle_requested", "body_clicked", "mpn_clicked", "empty_space_clicked",
    )

    checklist_view.unload()

    assert recorder.emissions == []


def test_checklist_view_unload_is_safe_unloaded_and_twice(checklist_view):
    checklist_view.unload()
    checklist_view.unload()
    assert not checklist_view.is_loaded()

    checklist_view.populate_section(_checklist_rows(2), "THT")
    checklist_view.unload()
    checklist_view.unload()
    assert checklist_view._index == {}


def test_checklist_view_reload_after_unload_matches_first_load(checklist_view):
    checklist_view.populate_section(_checklist_rows(5), "THT")
    first_item_count = checklist_view._layout.count()

    checklist_view.unload()
    checklist_view.populate_section(_checklist_rows(5), "THT")

    assert len(checklist_view._index) == 5
    assert checklist_view._layout.count() == first_item_count


# ---------------------------------------------------------------------------
# AuditBomPanel
# ---------------------------------------------------------------------------

@pytest.fixture
def bom_panel(qtbot, theme):
    layout_query_service = Mock(spec=LayoutQueryService)
    layout_query_service.list_bom_rows_for_audit.return_value = _bom_rows(4)
    panel = AuditBomPanel(layout_query_service, theme)
    qtbot.addWidget(panel)
    return panel


def test_bom_panel_unload_clears_rows_selection_and_header(bom_panel):
    bom_panel.load(audit_id=1)
    bom_panel.select_mpn("MPN-1")
    assert bom_panel.is_loaded()

    bom_panel.unload()

    assert bom_panel._row_index == {}
    assert bom_panel._selected_mpn is None
    assert bom_panel.header_label.text() == ""
    assert not bom_panel.header_label.isVisible()
    assert bom_panel.container_layout.count() == 0
    assert not bom_panel.is_loaded()


def test_bom_panel_unload_emits_nothing(bom_panel):
    bom_panel.load(audit_id=1)
    recorder = SignalRecorder()
    recorder.watch(bom_panel, "bom_row_clicked", "empty_space_clicked", "error_occurred")

    bom_panel.unload()

    assert recorder.emissions == []


def test_bom_panel_unload_is_safe_unloaded_and_twice(bom_panel):
    bom_panel.unload()
    bom_panel.unload()
    assert not bom_panel.is_loaded()


def test_bom_panel_reload_after_unload_matches_first_load(bom_panel):
    bom_panel.load(audit_id=1)
    first_keys = set(bom_panel._row_index)

    bom_panel.unload()
    bom_panel.load(audit_id=1)

    assert set(bom_panel._row_index) == first_keys
    assert bom_panel._selected_mpn is None


def test_bom_panel_clear_selection_is_not_teardown(bom_panel):
    """clear() was renamed clear_selection() because SelectionCoordinator calls
    it on every gesture. A pane carrying both a clear() and an unload() is a
    trap; this pins the distinction."""
    bom_panel.load(audit_id=1)
    bom_panel.select_mpn("MPN-1")

    bom_panel.clear_selection()

    assert bom_panel._selected_mpn is None
    assert bom_panel.is_loaded()
    assert len(bom_panel._row_index) == 4


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@pytest.fixture
def dashboard(qtbot, theme):
    checklist_service = Mock(spec=ChecklistService)
    checklist_service.load_active_audit.return_value = _active_view()
    d = Dashboard(
        checklist_service,
        Mock(spec=AuditSplitService),
        Mock(spec=CompletionService),
        Mock(spec=IngestionService),
        Mock(),
        Mock(),
        theme,
    )
    qtbot.addWidget(d)
    return d


def test_dashboard_unload_drops_view_and_audit_id(dashboard):
    dashboard.load(audit_id=1)
    assert dashboard.is_loaded()
    assert dashboard.current_audit_id() == 1

    dashboard.unload()

    assert dashboard._view is None
    assert dashboard._current_audit_id is None
    assert dashboard.current_audit_id() is None
    assert not dashboard.is_loaded()


def test_dashboard_unload_unloads_both_checklist_panes(dashboard):
    dashboard.load(audit_id=1)

    dashboard.unload()

    assert dashboard.checklist_tht._index == {}
    assert dashboard.checklist_notes._index == {}
    assert not dashboard.checklist_tht.is_loaded()
    assert not dashboard.checklist_notes.is_loaded()


def test_dashboard_unload_empties_actions_menu(dashboard):
    dashboard.load(audit_id=1)

    dashboard.unload()

    assert dashboard.actions_menu.isEmpty()


def test_dashboard_unload_releases_service_scoped_caches(dashboard):
    """Section 3.7: the ref-des index and BOM source-file memo outlive the audit
    unless unload() drops them. Called directly, not behind hasattr."""
    dashboard.load(audit_id=1)
    # load() routes through unload(), so count only the explicit teardown.
    dashboard._checklist_service.release_audit_scoped_caches.reset_mock()

    dashboard.unload()

    dashboard._checklist_service.release_audit_scoped_caches.assert_called_once()


def test_dashboard_unload_emits_nothing(dashboard):
    dashboard.load(audit_id=1)
    recorder = SignalRecorder()
    recorder.watch(
        dashboard,
        "metadata_changed", "exit_requested", "error_occurred",
        "tht_body_clicked", "tht_mpn_clicked", "empty_clicked",
    )

    dashboard.unload()

    assert recorder.emissions == []


def test_dashboard_unload_is_safe_unloaded_and_twice(dashboard):
    dashboard.unload()
    dashboard.unload()
    assert not dashboard.is_loaded()


# ---------------------------------------------------------------------------
# SelectionCoordinator
# ---------------------------------------------------------------------------

@pytest.fixture
def coordinator():
    return SelectionCoordinator(lambda: _active_view(), Mock(spec=LayoutQueryService))


def test_coordinator_unload_drops_active_selection(coordinator):
    coordinator._active = SelectionIntent(kind=SelectionKind.BOM_MPN, mpn="MPN-1")
    assert coordinator.is_loaded()

    coordinator.unload()

    assert coordinator._active is None
    assert not coordinator.is_loaded()


def test_coordinator_unload_keeps_lifetime_stable_pane_references(coordinator):
    """Section 3.3: _dashboard and _bom_panel are non-owning references to
    siblings that outlive the audit. Nulling them would break the next load."""
    dashboard_stub, bom_stub = Mock(), Mock()
    coordinator.register_dashboard(dashboard_stub)
    coordinator.register_bom_panel(bom_stub)
    coordinator._active = SelectionIntent(kind=SelectionKind.BOM_MPN, mpn="MPN-1")

    coordinator.unload()

    assert coordinator._dashboard is dashboard_stub
    assert coordinator._bom_panel is bom_stub


def test_coordinator_unload_emits_nothing(coordinator):
    """_emit_clear() both clears and emits. unload() must not be routed through
    it -- an emission during teardown re-enters a half-unloaded pane."""
    coordinator._active = SelectionIntent(kind=SelectionKind.BOM_MPN, mpn="MPN-1")
    recorder = SignalRecorder()
    recorder.watch(coordinator, "selection_changed")

    coordinator.unload()

    assert recorder.emissions == []


def test_coordinator_unload_is_safe_unloaded_and_twice(coordinator):
    coordinator.unload()
    coordinator.unload()
    assert not coordinator.is_loaded()


# ---------------------------------------------------------------------------
# LayoutCanvas
# ---------------------------------------------------------------------------

@pytest.fixture
def canvas(qtbot, theme):
    widget = LayoutCanvas(Mock(spec=LayoutQueryService), Mock(spec=PdfRenderer), theme=theme)
    qtbot.addWidget(widget)
    return widget


def _load_canvas_state(canvas) -> None:
    """Populate every audit-scoped structure unload() is required to release,
    without going through the off-thread render path."""
    canvas._current_audit_id = 7
    canvas._current_context = LayoutContext(
        audit_id=7,
        pdf_source_file_id=2,
        pdf_path=pathlib.Path("fake.pdf"),
        page_count=2,
        page_dimensions=((1000.0, 800.0), (1000.0, 800.0)),
    )
    canvas._current_page_index = 0
    canvas._pending_pdf = Mock()
    canvas._primary_pending = Mock()
    canvas._secondary_pending = Mock()
    canvas._active_source = "secondary"
    canvas._current_scale = 2.5
    canvas._coord_cache[0] = [HighlightCoord("R1", 0, 0.1, 0.1, 0.2, 0.2)]
    canvas._ensure_highlight_pool(6)
    canvas._raster_cache.put(
        CachedPage(page_index=0, target_pixel_height=100, pixmap=QPixmap(10, 10), byte_size=400),
        None,
    )
    canvas._page_switcher.set_page_count(2)


def test_canvas_current_audit_id_is_none_before_any_load(canvas):
    """F7: __init__ never created the field, so is_loaded() and the stale-height
    branch both raised AttributeError instead of reporting 'not loaded'."""
    assert canvas._current_audit_id is None
    assert not canvas.is_loaded()


def test_canvas_unload_releases_raster_cache_and_coord_cache(canvas):
    _load_canvas_state(canvas)
    assert canvas._raster_cache.total_bytes() > 0

    canvas.unload()

    assert canvas._raster_cache.total_bytes() == 0
    assert canvas._raster_cache.resident_page_count() == 0
    assert canvas._coord_cache == {}


def test_canvas_unload_releases_highlight_pool_from_the_scene(canvas):
    """F3: the pool was a monotonic high-water mark across every audit of the
    session, and every item stayed in the scene's spatial index."""
    _load_canvas_state(canvas)
    assert len(canvas._highlight_items) == 6
    scene_items_when_loaded = len(canvas._scene.items())

    canvas.unload()

    assert canvas._highlight_items == []
    assert len(canvas._scene.items()) == scene_items_when_loaded - 6


def test_canvas_unload_nulls_every_context_record(canvas):
    _load_canvas_state(canvas)

    canvas.unload()

    assert canvas._current_audit_id is None
    assert canvas._current_context is None
    assert canvas._current_page_index is None
    assert canvas._pending_pdf is None
    assert canvas._primary_pending is None
    assert canvas._secondary_pending is None
    assert canvas._last_intent is None
    assert canvas._last_resolved is None
    assert canvas._active_source == "primary"
    assert canvas._current_scale == 1.0
    assert not canvas.is_loaded()


def test_canvas_unload_resets_the_page_switcher(canvas):
    _load_canvas_state(canvas)
    assert canvas._page_switcher._page_count == 2

    canvas.unload()

    assert canvas._page_switcher._page_count == 0
    assert canvas._page_switcher._current_index == 0
    assert canvas._page_switcher._segment_buttons == []
    assert canvas._page_switcher.isHidden()


def test_canvas_unload_bumps_the_render_generation(canvas):
    """Invariant I1: bumping before releasing state is what lets a result from a
    job already in flight be discarded on arrival, with no blocking wait."""
    _load_canvas_state(canvas)
    epoch_before = canvas._render_epoch
    bumped: list[int] = []
    canvas.generation_bumped.connect(bumped.append)

    canvas.unload()

    assert canvas._render_epoch > epoch_before
    assert bumped == [canvas._render_epoch]


def test_canvas_unload_emits_no_signal_other_than_generation_bumped(canvas):
    _load_canvas_state(canvas)
    recorder = SignalRecorder()
    recorder.watch(canvas, "error_occurred", "refdes_clicked", "empty_clicked",
                   "request_render", "font_scale_change_requested")

    canvas.unload()

    assert recorder.emissions == []


def test_canvas_unload_is_safe_unloaded_and_twice(canvas):
    canvas.unload()
    canvas.unload()
    assert not canvas.is_loaded()

    _load_canvas_state(canvas)
    canvas.unload()
    canvas.unload()
    assert canvas._raster_cache.total_bytes() == 0


def test_canvas_page_changed_after_unload_is_inert(canvas):
    """Invariant I3: the switcher is a child widget and can deliver a queued
    page_changed into an unloaded canvas."""
    _load_canvas_state(canvas)
    canvas.unload()

    canvas._on_page_changed(1)

    assert canvas._current_page_index is None


def test_canvas_reissue_for_settled_viewport_after_unload_is_inert(canvas):
    """Invariant I3, first row: a timeout already posted when stop() was called
    still fires. unload() must leave the guard conditions true."""
    _load_canvas_state(canvas)
    canvas.unload()
    submitted: list[object] = []
    canvas.request_render.connect(submitted.append)

    canvas.reissue_for_settled_viewport()

    assert submitted == []


# ---------------------------------------------------------------------------
# AuditView -- the orchestrator
# ---------------------------------------------------------------------------

@pytest.fixture
def audit_view_theme(theme):
    """The container reads two layout blocks the per-pane fixtures never touch."""
    return Theme.for_testing(
        canvas=theme._canvas,
        left_panel={**theme._left_panel, "layout": {"min_width_px": 380}},
        bom_panel={
            **theme._bom_panel,
            "layout": {"min_width_percent": 0.15, "min_width_absolute_px": 240},
        },
    )


@pytest.fixture
def audit_view(qtbot, audit_view_theme):
    from cockpit.ingestion.service import IngestionService as _IngestionService
    from cockpit.services.release import ReleaseService
    from cockpit.services.setup_bom import SetupBomService
    from cockpit.ui.widgets.audit_view import AuditView

    checklist_service = Mock(spec=ChecklistService)
    checklist_service.load_active_audit.return_value = _active_view()
    layout_query_service = Mock(spec=LayoutQueryService)
    layout_query_service.list_bom_rows_for_audit.return_value = _bom_rows(3)

    view = AuditView(
        checklist_service,
        Mock(spec=AuditSplitService),
        Mock(spec=CompletionService),
        Mock(spec=_IngestionService),
        layout_query_service,
        Mock(spec=ReleaseService),
        Mock(spec=SetupBomService),
        Mock(spec=PdfRenderer),
        theme=audit_view_theme,
    )
    qtbot.addWidget(view)
    return view


def test_audit_view_unload_discharges_every_pane(audit_view):
    audit_view.load(1)
    assert audit_view.is_loaded()

    audit_view.unload()

    assert not audit_view.is_loaded()
    assert audit_view.current_audit_id() is None
    assert not audit_view._dashboard.is_loaded()
    assert not audit_view._bom_panel.is_loaded()
    assert not audit_view._layout_canvas.is_loaded()
    assert not audit_view._coordinator.is_loaded()
    assert audit_view._metadata_layout.count() == 0


def test_audit_view_unload_does_not_refilter_the_panes_it_just_tore_down(audit_view):
    """Post-condition (d) at the orchestrator. QLineEdit.clear() emits
    textChanged, which routes into Dashboard.apply_filter and
    AuditBomPanel.apply_filter -- re-entering two panes that unload() has
    already released. It survived only because both indices are empty by then,
    which is a coincidence of ordering, not a guarantee."""
    audit_view.load(1)
    audit_view.search_input.setText("R1")
    filtered: list[str] = []
    audit_view._dashboard.apply_filter = lambda q: filtered.append(q)
    audit_view._bom_panel.apply_filter = lambda q: filtered.append(q)

    audit_view.unload()

    assert audit_view.search_input.text() == ""
    assert filtered == []


def test_audit_view_unload_leaves_the_search_box_signalling_afterwards(audit_view):
    """blockSignals is scoped to the clear, not left latched on the widget."""
    audit_view.load(1)
    audit_view.unload()
    seen: list[str] = []
    audit_view.search_input.textChanged.connect(seen.append)

    audit_view.search_input.setText("C4")

    assert seen == ["C4"]


def test_audit_view_load_routes_through_unload(audit_view):
    """Section 3.2: one release path, whatever the reason for release. The three
    ad-hoc clear prologues collapse into it."""
    audit_view.load(1)
    audit_view._layout_canvas._raster_cache.put(
        CachedPage(page_index=0, target_pixel_height=100, pixmap=QPixmap(10, 10), byte_size=400),
        None,
    )

    audit_view.load(2)

    assert audit_view._layout_canvas._raster_cache.total_bytes() == 0
    assert audit_view.current_audit_id() == 2


def test_audit_view_unload_is_safe_unloaded_and_twice(audit_view):
    audit_view.unload()
    audit_view.load(1)
    audit_view.unload()
    audit_view.unload()

    assert not audit_view.is_loaded()


def test_discard_if_showing_only_fires_for_the_displayed_audit(audit_view):
    audit_view.load(1)

    assert audit_view.discard_if_showing(2) is False
    assert audit_view.is_loaded()
    assert audit_view.discard_if_showing(1) is True
    assert not audit_view.is_loaded()
