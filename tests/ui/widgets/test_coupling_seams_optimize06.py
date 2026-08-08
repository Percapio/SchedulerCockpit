"""F9 and F11 -- bounded shutdown and the four coupling reach-throughs
(Optimize06 sections 2.9, 2.11, 3.5, 3.8; step 5 gate).

Each of the four reach-throughs is asserted as a property of the seam that
replaced it, not by inspecting the call site. A reach-through that is deleted
but leaves no owned accessor behind grows back at the next feature phase --
which is how there came to be four.
"""

import inspect
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QWidget

from cockpit.layout.renderer import PdfRenderer
from cockpit.persistence.types import AuditStatus
from cockpit.services.checklist import ChecklistService
from cockpit.services.completion import CompletionService
from cockpit.services.layout_query import LayoutQueryService
from cockpit.services.split import AuditSplitService
from cockpit.services.views import ActiveAuditView
from cockpit.ingestion.service import IngestionService
from cockpit.ui.canvas.layout_canvas import LayoutCanvas
from cockpit.ui.theme import Theme
from cockpit.ui.widgets.dashboard import Dashboard
from cockpit.ui.widgets.open_audit_picker import OpenAuditPicker


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
    )


def _active_view(has_pdf: bool) -> ActiveAuditView:
    return ActiveAuditView(
        audit_id=1,
        part_number="PN-123",
        work_order_ref="WO-1",
        split_suffix=None,
        quantity=10,
        status=AuditStatus.NOT_CLEAR,
        split_reason=None,
        traveler_metadata=None,
        has_pdf=has_pdf,
        tht_placement_count=0,
    )


# ---------------------------------------------------------------------------
# F11 item 3 -- MainWindow set a thread-safety flag two levels down
# ---------------------------------------------------------------------------

@pytest.fixture
def canvas(qtbot, theme):
    widget = LayoutCanvas(Mock(spec=LayoutQueryService), Mock(spec=PdfRenderer), theme=theme)
    qtbot.addWidget(widget)
    return widget


def test_canvas_declines_to_submit_when_no_worker_is_attached(canvas):
    submitted: list[object] = []
    canvas.request_render.connect(submitted.append)
    canvas.set_render_worker_alive(False)

    canvas._submit_render(Mock())

    assert submitted == []


def test_canvas_submits_once_a_worker_is_attached(canvas):
    submitted: list[object] = []
    canvas.request_render.connect(submitted.append)
    canvas.set_render_worker_alive(True)
    job = Mock()

    canvas._submit_render(job)

    assert submitted == [job]


def test_worker_alive_defaults_to_false_before_any_worker_is_wired(canvas):
    """A canvas with no worker must not queue jobs nobody will answer."""
    assert canvas._worker_alive is False


def test_audit_view_exposes_the_worker_flag_so_main_window_need_not_reach(qtbot):
    """Section 3.5: set_render_worker_alive is public on AuditView, delegating
    to the canvas."""
    from cockpit.ui.widgets.audit_view import AuditView

    assert hasattr(AuditView, "set_render_worker_alive")
    assert hasattr(LayoutCanvas, "set_render_worker_alive")


# ---------------------------------------------------------------------------
# F11 items 1 and 2 -- AuditView reached into Dashboard's private fields
# ---------------------------------------------------------------------------

@pytest.fixture
def dashboard(qtbot, theme):
    service = Mock(spec=ChecklistService)
    service.load_active_audit.return_value = _active_view(has_pdf=True)
    d = Dashboard(
        service,
        Mock(spec=AuditSplitService),
        Mock(spec=CompletionService),
        Mock(spec=IngestionService),
        Mock(),
        Mock(),
        theme,
    )
    qtbot.addWidget(d)
    return d


def test_dashboard_owns_current_audit_id(dashboard):
    assert dashboard.current_audit_id() is None

    dashboard.load(audit_id=42)

    assert dashboard.current_audit_id() == 42


def test_dashboard_owns_has_pdf(dashboard):
    """AuditView._has_pdf read _dashboard._view.has_pdf, so it was coupled to a
    private field name on another class."""
    dashboard.load(audit_id=42)

    assert dashboard.has_pdf() is True


def test_dashboard_has_pdf_is_false_when_nothing_is_loaded(dashboard):
    """The reach-through version raised or misreported here, depending on which
    of _view and _current_audit_id had been nulled."""
    assert dashboard.has_pdf() is False

    dashboard.load(audit_id=42)
    dashboard.unload()

    assert dashboard.has_pdf() is False


def test_dashboard_has_pdf_reports_false_for_an_audit_without_one(dashboard):
    dashboard._checklist_service.load_active_audit.return_value = _active_view(has_pdf=False)

    dashboard.load(audit_id=42)

    assert dashboard.has_pdf() is False


def test_forget_audit_is_gone_and_discard_if_showing_replaced_it(qtbot):
    """forget_audit nulled two of Dashboard's fields from outside Dashboard.
    It was deleted in step 3 but MainWindow still called it, so this pins both
    halves."""
    from cockpit.ui.widgets.audit_view import AuditView

    assert not hasattr(AuditView, "forget_audit")
    assert hasattr(AuditView, "discard_if_showing")


def test_main_window_does_not_call_forget_audit(qtbot):
    from cockpit.ui.main_window import MainWindow

    source = inspect.getsource(MainWindow._invalidate_audit_view_if_loaded)

    assert "forget_audit" not in source
    assert "discard_if_showing" in source


# ---------------------------------------------------------------------------
# F11 item 4 -- the picker duck-typed a private attribute on window()
# ---------------------------------------------------------------------------

def test_picker_emits_intent_and_resolves_no_service(qtbot):
    """Section 3.8: the picker inspects no ancestor widget and holds no holiday
    service. The failure mode of the old version was a log line and nothing
    visible to the operator."""
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    emitted: list[bool] = []
    picker.holidays_requested.connect(lambda: emitted.append(True))

    picker._on_holidays_clicked()

    assert emitted == [True]


def test_picker_source_contains_no_window_back_channel(qtbot):
    source = inspect.getsource(OpenAuditPicker._on_holidays_clicked)

    assert "self.window()" not in source
    assert "hasattr" not in source
    assert "_holiday_svc" not in source


def test_picker_does_not_import_the_holiday_dialog():
    """Optimize04 section 1 kept the picker service-free. Owning the dialog
    import is the first step back toward owning the service."""
    import cockpit.ui.widgets.open_audit_picker as picker_module

    assert not hasattr(picker_module, "HolidayDialog")


# ---------------------------------------------------------------------------
# F9 -- bounded render-thread shutdown
# ---------------------------------------------------------------------------

def test_shutdown_render_thread_reports_a_clean_exit():
    from cockpit.ui.main_window import MainWindow

    window = Mock()  # not spec'd: the method reads _audit_view and _render_thread
    window._render_thread = Mock()
    window._render_thread.wait.return_value = True

    exited = MainWindow.shutdown_render_thread(window, timeout_ms=250)

    assert exited is True
    window._audit_view.set_render_worker_alive.assert_called_once_with(False)
    window._render_thread.quit.assert_called_once()
    window._render_thread.wait.assert_called_once_with(250)


def test_shutdown_render_thread_bounds_the_wait_and_does_not_terminate():
    """F9 was an unbounded wait(). The first repair replaced it with
    terminate() plus a SECOND unbounded wait() -- killing a thread inside fitz,
    then blocking on it. Section 3.5 says leave it running and exit anyway."""
    from cockpit.ui.main_window import MainWindow

    window = Mock()  # not spec'd: the method reads _audit_view and _render_thread
    window._render_thread = Mock()
    window._render_thread.wait.return_value = False

    exited = MainWindow.shutdown_render_thread(window, timeout_ms=250)

    assert exited is False
    window._render_thread.wait.assert_called_once_with(250)
    window._render_thread.terminate.assert_not_called()


def test_shutdown_render_thread_is_safe_with_no_thread():
    from cockpit.ui.main_window import MainWindow

    window = Mock()  # not spec'd: the method reads _audit_view and _render_thread
    window._render_thread = None

    assert MainWindow.shutdown_render_thread(window, timeout_ms=250) is True
    window._audit_view.set_render_worker_alive.assert_called_once_with(False)


def test_shutdown_timeout_default_is_bounded_and_positive():
    from cockpit.ui.main_window import RENDER_SHUTDOWN_TIMEOUT_MS, MainWindow

    default = inspect.signature(MainWindow.shutdown_render_thread).parameters["timeout_ms"].default

    assert default == RENDER_SHUTDOWN_TIMEOUT_MS
    assert 0 < RENDER_SHUTDOWN_TIMEOUT_MS <= 30_000


# ---------------------------------------------------------------------------
# F10 -- the values that had to become configurable
# ---------------------------------------------------------------------------

def test_render_tokens_are_sourced_from_the_theme(theme, qtbot):
    canvas = LayoutCanvas(Mock(spec=LayoutQueryService), Mock(spec=PdfRenderer), theme=theme)
    qtbot.addWidget(canvas)

    assert canvas._render_budget.max_cached_bytes == theme.canvas_render_max_cached_bytes()
    assert canvas._render_budget.prefetch_page_limit == theme.canvas_render_prefetch_page_limit()
    assert canvas._resize_debouncer.interval() == theme.canvas_render_resize_debounce_ms()


def test_install_dir_log_duplication_is_opt_in_and_off_by_default():
    """Section 2.12: every record was formatted and written twice, and in a
    frozen install the second copy landed in the installation directory."""
    from cockpit.ui.config import AppConfig

    default = AppConfig.__dataclass_fields__["log_duplicate_to_install_dir"].default

    assert default is False


def test_idle_maintenance_interval_is_configurable():
    from cockpit.ui.config import AppConfig

    assert "idle_maintenance_interval_ms" in AppConfig.__dataclass_fields__
