import sys
import os
import gc
import psutil
import argparse
import time
import json
import dataclasses
import pathlib
import inspect
import datetime
from typing import Optional, Dict, List, Set, Any, Tuple

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QSettings, QEvent, QTimer
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtCore import QPointF, QPoint, Qt

# Ensure cockpit is importable
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent.absolute()))

from cockpit.ui.config import AppConfig
from cockpit.ui.bootstrap import bootstrap
from cockpit.ui.main_window import MainWindow
from cockpit.ui.theme import ThemeLoader
from cockpit.ui.ui_prefs import StyleController, RuntimeCalcSettingsController
from cockpit.ui.data_migration import migrate_to_versioned_layout

from cockpit.ui.widgets.checklist_row import ChecklistRow
from cockpit.ui.widgets.audit_bom_panel import AuditBomRow
from cockpit.ui.widgets.dashboard import Dashboard
from cockpit.services.views import ActiveAuditView
from cockpit.ui.widgets.component_row import RefDesChip, ComponentRowCore
from cockpit.ui.canvas.layout_canvas import LayoutCanvas, HighlightItem
from cockpit.ui.widgets.audit_view import AuditView

from scripts.audit.churn_fixture import seed_churn_fixture, ChurnFixture

# ==============================================================================
# ERRORS
# ==============================================================================
class ProbeUnavailable(Exception):
    def __init__(self, candidate_fields, target_class, reason):
        super().__init__(f"ProbeUnavailable: {reason} on {target_class} for fields {candidate_fields}")
        self.candidate_fields = candidate_fields
        self.target_class = target_class
        self.reason = reason

class GestureUnavailable(Exception):
    def __init__(self, pane, missing_seam, rows_present):
        super().__init__(f"GestureUnavailable in {pane}: missing seam '{missing_seam}', {rows_present} rows present.")
        self.pane = pane
        self.missing_seam = missing_seam
        self.rows_present = rows_present

class DrainTimeout(Exception):
    def __init__(self, cycle_index, waited_ms, outstanding_jobs, latest_generation):
        super().__init__(f"DrainTimeout at cycle {cycle_index}: waited {waited_ms}ms, {outstanding_jobs} jobs pending at gen {latest_generation}")

class BaselineMissing(Exception):
    def __init__(self, searched_root, required_probe, required_viewport):
        super().__init__(f"BaselineMissing in {searched_root} for probe {required_probe} at {required_viewport}px")

class ConstructionParityBroken(Exception):
    def __init__(self, message):
        super().__init__(message)

# ==============================================================================
# RECORDS
# ==============================================================================
@dataclasses.dataclass(frozen=True)
class RasterReading:
    resident_bytes: int
    resident_page_count: int
    highlight_pool_size: int
    probe_implementation: str

@dataclasses.dataclass(frozen=True)
class GestureOutcome:
    pane: str
    row_key: Any
    ref_des: str
    chip_emitted: bool

@dataclasses.dataclass(frozen=True)
class ChurnSample:
    cycle_index: int
    rss_bytes: int
    python_allocated_bytes: int
    gc_uncollectable_count: int
    raster_at_peak: RasterReading
    raster_after_exit: RasterReading
    live_counts_at_peak: Dict[str, int]
    live_counts_after_exit: Dict[str, int]
    # Read before the DeferredDelete flush.
    live_counts_before_deferred_delete: Dict[str, int]
    rss_before_deferred_delete: int
    rss_after_exit: int
    # Read after the DeferredDelete flush but BEFORE any gc.collect(). This is
    # the only figure that can distinguish "reclaimed by refcount at the cycle
    # the layout dropped it" from "reclaimed by a later cycle-collection pass",
    # which is exactly what step 4 (F4) is about. live_counts_after_exit is
    # taken post-collect and cannot tell the two apart.
    live_counts_before_gc: Dict[str, int]
    render_jobs_observed: int
    quiescence_reached: bool
    exit_wall_ms: float
    canvas_viewport_height: int

@dataclasses.dataclass(frozen=True)
class AcceptanceBreach:
    criterion_id: str
    first_cycle: int
    observed: str
    threshold: str

@dataclasses.dataclass(frozen=True)
class BreachLedger:
    entries: Tuple[AcceptanceBreach, ...]

@dataclasses.dataclass(frozen=True)
class ChurnReport:
    tree_revision: str
    probe_implementation: str
    canvas_viewport_height: int
    captured_at_utc: str
    cycles_requested: int
    samples: Tuple[ChurnSample, ...]
    breaches: BreachLedger

    def to_json(self):
        return json.dumps(dataclasses.asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str):
        d = json.loads(data)
        d['samples'] = tuple(ChurnSample(**s) for s in d['samples'])
        d['breaches'] = BreachLedger(tuple(AcceptanceBreach(**b) for b in d['breaches']['entries']))
        return cls(**d)

# ==============================================================================
# PROBES
# ==============================================================================
class PixmapDictProbe:
    def __init__(self, canvas: LayoutCanvas):
        self.canvas = canvas

    def read(self) -> RasterReading:
        if not hasattr(self.canvas, '_pixmap_cache'):
            raise ProbeUnavailable(('_pixmap_cache',), 'LayoutCanvas', 'NONE_PRESENT')
        if not hasattr(self.canvas, '_highlight_items'):
            raise ProbeUnavailable(('_highlight_items',), 'LayoutCanvas', 'NONE_PRESENT')
            
        resident_bytes = 0
        for target_h, px in self.canvas._pixmap_cache.values():
            if px.width() == 0 or px.height() == 0:
                raise ProbeUnavailable(('_pixmap_cache',), 'LayoutCanvas', 'PIXMAP_ZERO_AREA')
            resident_bytes += px.width() * px.height() * 4
            
        return RasterReading(
            resident_bytes=resident_bytes,
            resident_page_count=len(self.canvas._pixmap_cache),
            highlight_pool_size=len(self.canvas._highlight_items),
            probe_implementation="pixmap-dict"
        )
        
    def implementation_name(self) -> str:
        return "pixmap-dict"

class RasterPageCacheProbe:
    def __init__(self, canvas: LayoutCanvas):
        self.canvas = canvas

    def read(self) -> RasterReading:
        if not hasattr(self.canvas, '_raster_cache'):
            raise ProbeUnavailable(('_raster_cache',), 'LayoutCanvas', 'NONE_PRESENT')
        if not hasattr(self.canvas, '_highlight_items'):
            raise ProbeUnavailable(('_highlight_items',), 'LayoutCanvas', 'NONE_PRESENT')
            
        return RasterReading(
            resident_bytes=self.canvas._raster_cache.total_bytes(),
            resident_page_count=self.canvas._raster_cache.resident_page_count(),
            highlight_pool_size=len(self.canvas._highlight_items),
            probe_implementation="raster-page-cache"
        )

    def implementation_name(self) -> str:
        return "raster-page-cache"

def select_raster_probe(canvas: LayoutCanvas):
    has_pixmap = hasattr(canvas, '_pixmap_cache')
    has_cache = hasattr(canvas, '_raster_cache')
    if has_pixmap and has_cache:
        raise ProbeUnavailable(('_pixmap_cache', '_raster_cache'), 'LayoutCanvas', 'MORE_THAN_ONE_PRESENT')
    if not has_pixmap and not has_cache:
        raise ProbeUnavailable(('_pixmap_cache', '_raster_cache'), 'LayoutCanvas', 'NONE_PRESENT')
        
    if has_pixmap:
        return PixmapDictProbe(canvas)
    return RasterPageCacheProbe(canvas)

# ==============================================================================
# QUIESCENCE OBSERVER
# ==============================================================================
class RenderObserver:
    def __init__(self, canvas: LayoutCanvas, worker):
        self.canvas = canvas
        self.worker = worker
        self.jobs_submitted = 0
        self.jobs_answered = 0
        self.last_answer_time = 0.0
        
        # Connect to canvas (GUI thread -> same thread)
        self.canvas.request_render.connect(self._on_submit)
        
        # Connect to worker (Worker -> GUI queued)
        self.worker.render_ready.connect(self._on_answer)
        self.worker.render_error.connect(self._on_answer)
        
    def _on_submit(self, *args):
        self.jobs_submitted += 1
        
    def _on_answer(self, *args):
        self.jobs_answered += 1
        self.last_answer_time = time.time()
        
    def outstanding_jobs(self):
        return max(0, self.jobs_submitted - self.jobs_answered)

    def reset_cycle(self):
        """Both counters, or outstanding_jobs() reads 0 for the whole cycle."""
        self.jobs_submitted = 0
        self.jobs_answered = 0

# Global observer state
_quiescence_window_ms = 1000
_render_observer: Optional[RenderObserver] = None
_render_durations = []

def bind_render_observer(window: MainWindow) -> RenderObserver:
    """Attach the submission/answer counters before the first cycle runs.

    The worker is owned by MainWindow, not by the canvas. Absence is a
    structured failure, never a neutral reading (Patch01 section 2).
    """
    global _render_observer
    if _render_observer is None:
        worker = getattr(window, '_render_worker', None)
        if worker is None:
            raise ProbeUnavailable(('_render_worker',), 'MainWindow', 'NONE_PRESENT')
        _render_observer = RenderObserver(window._audit_view._layout_canvas, worker)
    return _render_observer

def await_renders_answered(window: MainWindow, cycle_index: int, timeout_ms: int = 10000) -> None:
    """Wait until every job submitted so far this cycle has been answered.

    Weaker than quiescence — no quiet window — because the peak read only needs
    the pages it asked for to be resident, not proof the worker is idle.
    """
    observer = bind_render_observer(window)
    start = time.time()
    while observer.outstanding_jobs() > 0:
        QApplication.processEvents()
        if (time.time() - start) * 1000 > timeout_ms:
            raise DrainTimeout(
                cycle_index, timeout_ms, observer.outstanding_jobs(),
                window._audit_view._layout_canvas._render_epoch
            )
        time.sleep(0.01)
    QApplication.processEvents()

def pump_until_renders_land(window: MainWindow, timeout_ms: int = 5000) -> None:
    """Pump the event loop until nothing is outstanding, or the timeout expires.

    Deliberately non-raising: a job superseded by a newer generation is never
    answered, and that is correct behaviour, not a drain failure.
    """
    observer = bind_render_observer(window)
    start = time.time()
    while observer.outstanding_jobs() > 0 and (time.time() - start) * 1000 < timeout_ms:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()

def raster_budget_ceiling(window: MainWindow) -> Optional[int]:
    """The A3 ceiling, or None on a tree that predates step 2 and has none."""
    budget = getattr(window._audit_view._layout_canvas, '_render_budget', None)
    return None if budget is None else budget.max_cached_bytes

def await_render_quiescence(window: MainWindow, cycle_index: int) -> bool:
    global _quiescence_window_ms, _render_observer

    canvas = window._audit_view._layout_canvas
    bind_render_observer(window)

    if _render_observer.jobs_submitted == 0:
        raise GestureUnavailable("canvas", "No render jobs observed", 0)
        
    start_wait = time.time()
    
    # Warmup calibration logic
    if cycle_index == 1 and _render_observer.jobs_answered > 0:
        max_duration = max(_render_durations) if _render_durations else 300.0
        _quiescence_window_ms = max(250.0, min(10000.0 / 2, max_duration * 3.0))
        
    while True:
        QApplication.processEvents()
        
        outstanding = _render_observer.outstanding_jobs()
        ms_since_last_answer = (time.time() - _render_observer.last_answer_time) * 1000
        
        if outstanding == 0 and ms_since_last_answer >= _quiescence_window_ms:
            # We assume it is drained.
            break
            
        if (time.time() - start_wait) * 1000 > 10000:
            raise DrainTimeout(cycle_index, 10000, outstanding, canvas._render_epoch)
            
        time.sleep(0.01)
        
    return True

# ==============================================================================
# HELPERS
# ==============================================================================
def assert_construction_parity(observed_arguments: Set[str]) -> None:
    sig = inspect.signature(MainWindow.__init__)
    # Ignore self
    declared = set(sig.parameters.keys()) - {'self', 'args', 'kwargs'}
    
    missing = declared - observed_arguments
    extra = observed_arguments - declared
    
    if missing or extra:
        raise ConstructionParityBroken(f"MainWindow parity broken. Missing: {missing}. Extra: {extra}.")

def open_audit(window: MainWindow, audit_id: int):
    window._on_picker_audit_selected(audit_id)
    start = time.time()
    while window.stacked.currentWidget() != window._audit_view and time.time() - start < 5.0:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()

def exit_to_list(window: MainWindow) -> float:
    start_exit = time.time()
    window._audit_view.exit_requested.emit()
    QApplication.processEvents()
    return time.time() - start_exit

def switch_pages(window: MainWindow, count: int):
    """Advance the displayed page `count` times through whichever control the
    switcher is currently showing.

    A document above the segmented threshold renders as a pager with no segment
    buttons; driving only the segments makes this a silent no-op on exactly the
    multi-page reference document F1 is about.
    """
    switcher = window._audit_view._layout_canvas._page_switcher
    if switcher._page_count <= 1:
        raise GestureUnavailable("page_switcher", "no multi-page document displayed", switcher._page_count)

    for _ in range(count):
        if switcher._segment_buttons:
            next_idx = 1 if switcher._current_index == 0 else 0
            if next_idx < len(switcher._segment_buttons):
                switcher._segment_buttons[next_idx].click()
        elif switcher._pager_next is not None and switcher._pager_prev is not None:
            at_last = switcher._current_index >= switcher._page_count - 1
            (switcher._pager_prev if at_last else switcher._pager_next).click()
        else:
            raise GestureUnavailable("page_switcher", "neither segments nor pager present", switcher._page_count)
        # Let the page land before switching again. Back-to-back clicks bump the
        # render generation and supersede each other, so the cache never fills
        # and the eviction path goes unexercised. Non-fatal: a superseded job is
        # legitimate, so this pumps rather than asserting.
        pump_until_renders_land(window)

def toggle_to_reference_source(window: MainWindow, expected_page_count: int):
    view = window._audit_view
    canvas = view._layout_canvas
    if not hasattr(canvas, '_pdf_toggle_btn') or not canvas._pdf_toggle_btn.isVisible() or not canvas._pdf_toggle_btn.isEnabled():
        raise GestureUnavailable("toolbar", "_pdf_toggle_btn missing or disabled", 0)
        
    canvas._pdf_toggle_btn.click()
    QApplication.processEvents()
    
    # Wait for document to load and page count to match
    start = time.time()
    while time.time() - start < 15.0:
        QApplication.processEvents()
        switcher = view._layout_canvas._page_switcher
        if switcher._page_count == expected_page_count:
            return
        time.sleep(0.01)
    raise GestureUnavailable("toolbar", f"reference document failed to load {expected_page_count} pages", len(switcher._segment_buttons))

def zoom_in_and_out(window: MainWindow, steps: int):
    canvas = window._audit_view._layout_canvas
    for _ in range(steps):
        wheel = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 120), QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollBegin, False)
        canvas.wheelEvent(wheel)
    QApplication.processEvents()
    for _ in range(steps):
        wheel = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, -120), QPoint(0, -120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollBegin, False)
        canvas.wheelEvent(wheel)
    QApplication.processEvents()

def read_live_counts() -> Dict[str, int]:
    objects = gc.get_objects()
    counts = {
        "ChecklistRow": sum(1 for o in objects if isinstance(o, ChecklistRow)),
        "AuditBomRow": sum(1 for o in objects if isinstance(o, AuditBomRow)),
        "RefDesChip": sum(1 for o in objects if isinstance(o, RefDesChip)),
        "ComponentRowCore": sum(1 for o in objects if isinstance(o, ComponentRowCore)),
        "HighlightItem": sum(1 for o in objects if isinstance(o, HighlightItem)),
        "AuditView": sum(1 for o in objects if isinstance(o, AuditView)),
        "ActiveAuditView": sum(1 for o in objects if isinstance(o, ActiveAuditView))
    }
    
    if counts["ChecklistRow"] > 0:
        import sys
        if "--trace-referrers" in sys.argv:
            target = [o for o in objects if isinstance(o, ChecklistRow)][0]
            refs = gc.get_referrers(target)
            print(f"ChecklistRow leaked! Found {len(refs)} referrers:")
            for idx, ref in enumerate(refs):
                print(f"[{idx}] {type(ref)}")
                if isinstance(ref, dict):
                    print(f"    Dict keys: {list(ref.keys())}")
                elif isinstance(ref, list):
                    print(f"    List len: {len(ref)}")
            sys.exit(1)
            
    del objects
    return counts

def drive_selection_gestures(window: MainWindow, fixture: ChurnFixture, count: int) -> List[GestureOutcome]:
    outcomes = []
    bom_panel = window._audit_view._bom_panel
    rows = list(bom_panel._row_index.values())
    if not rows:
        raise GestureUnavailable("BOM", "no rows present", 0)
        
    for i in range(count):
        if i == count - 1:
            # By MPN
            target_mpn = fixture.highlight_group_mpn
            target_row = next((r for r in rows if r._view.component_mpn == target_mpn), None)
            if not target_row:
                raise GestureUnavailable("BOM", f"target mpn {target_mpn} missing", len(rows))
        else:
            target_row = rows[i % len(rows)]
            
        if not hasattr(target_row, 'chips') or not target_row.chips:
            # ComponentRowCore has chips, AuditBomRow doesn't have it directly if not exposed.
            # Assuming row has `_core.chips` or `chips` is available.
            if hasattr(target_row, 'core') and target_row.core.chips:
                chip_map = target_row.core.chips
            elif hasattr(target_row, '_core') and target_row._core.chips:
                chip_map = target_row._core.chips
            elif hasattr(target_row, 'chips'):
                chip_map = target_row.chips
            else:
                raise GestureUnavailable("BOM", "chips map missing", len(rows))
                
        chip = next(iter(chip_map.values()))
        evt = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(0, 0), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        chip.mousePressEvent(evt)
        QApplication.processEvents()
        
        outcomes.append(GestureOutcome(pane="BOM", row_key=target_row._view.component_mpn, ref_des="N/A", chip_emitted=True))
        
    return outcomes

# ==============================================================================
# RUN CYCLE
# ==============================================================================
def run_churn_cycle(window: MainWindow, fixture: ChurnFixture, cycle_index: int) -> ChurnSample:
    global _render_observer
    bind_render_observer(window).reset_cycle()

    # Secondary visit
    if cycle_index % 5 == 0:
        sec_id = fixture.secondary_audit_ids[(cycle_index // 5) % len(fixture.secondary_audit_ids)]
        open_audit(window, sec_id)
        exit_to_list(window)
        
    # Main visit
    open_audit(window, fixture.composite_audit_id)
    # The page switcher is populated by accept_render_result, which is off-thread.
    # Switching pages before the first render lands drives an empty control.
    await_renders_answered(window, cycle_index)
    switch_pages(window, count=2)
    drive_selection_gestures(window, fixture, count=20)
    toggle_to_reference_source(window, expected_page_count=fixture.reference_page_count)
    # Walk the whole reference document, not two pages of it. At any supported
    # viewport reference_page_count pages exceed max_cached_bytes, so this is
    # what drives the cache into eviction -- the path A3 exists to bound.
    switch_pages(window, count=fixture.reference_page_count)

    # Peak is read while the REFERENCE source is displayed (Patch01 section 6.2).
    # Reading it after a toggle back to the 2-page primary understates the peak
    # by roughly reference_page_count / 2 and never observes F1's unbounded case.
    await_renders_answered(window, cycle_index)

    probe = select_raster_probe(window._audit_view._layout_canvas)
    reading_at_peak = probe.read()

    if reading_at_peak.highlight_pool_size == 0:
        raise ProbeUnavailable(('_highlight_pool',), 'LayoutCanvas', 'F3 Pool Not Populated')
        
    counts_at_peak = read_live_counts()
    
    zoom_in_and_out(window, steps=5)
    exit_wall_ms = exit_to_list(window) * 1000.0
    
    quiesced = await_render_quiescence(window, cycle_index)
    
    counts_before_deferred_delete = read_live_counts()
    rss_before_deferred_delete = psutil.Process(os.getpid()).memory_info().rss
    
    # Dispatch the queued DeferredDelete events. This is Qt destroying the C++
    # objects purge_widget_subtree scheduled -- it is NOT garbage collection,
    # and it must happen before either count is read.
    import PyQt6.QtCore
    PyQt6.QtCore.QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    # A1b: refcount alone must have reclaimed the wrappers by now. Read before
    # collecting, or a reference cycle is indistinguishable from no cycle.
    counts_before_gc = read_live_counts()

    gc.collect()

    reading_after_exit = probe.read()
    counts_after_exit = read_live_counts()

    process = psutil.Process(os.getpid())
    rss_bytes = process.memory_info().rss
    rss_after_exit = rss_bytes
    python_allocated_bytes = sys.getallocatedblocks() * 512 # Approx block size
    
    return ChurnSample(
        cycle_index=cycle_index,
        rss_bytes=rss_bytes,
        python_allocated_bytes=python_allocated_bytes,
        gc_uncollectable_count=len(gc.garbage),
        raster_at_peak=reading_at_peak,
        raster_after_exit=reading_after_exit,
        live_counts_at_peak=counts_at_peak,
        live_counts_after_exit=counts_after_exit,
        live_counts_before_deferred_delete=counts_before_deferred_delete,
        rss_before_deferred_delete=rss_before_deferred_delete,
        rss_after_exit=rss_after_exit,
        live_counts_before_gc=counts_before_gc,
        render_jobs_observed=_render_observer.jobs_submitted if _render_observer else 0,
        quiescence_reached=quiesced,
        exit_wall_ms=exit_wall_ms,
        canvas_viewport_height=window._audit_view._layout_canvas.height()
    )

def run_baseline(cycles: int, window: MainWindow, fixture: ChurnFixture) -> ChurnReport:
    samples = []
    breaches = []
    
    probe = select_raster_probe(window._audit_view._layout_canvas)
    probe_name = probe.implementation_name()
    ceiling = raster_budget_ceiling(window)
    bind_render_observer(window)

    for cycle in range(1, cycles + 1):
        try:
            print(f"--- STARTING CYCLE {cycle} ---", flush=True)
            sample = run_churn_cycle(window, fixture, cycle)
            print(f"--- FINISHED CYCLE {cycle} ---", flush=True)
            samples.append(sample)
            
            # Check A1
            if cycle > 1:
                # AuditView should be exactly 1
                for k, v in sample.live_counts_after_exit.items():
                    target = 1 if k == 'AuditView' else 0
                    if v > target:
                        if not any(b.criterion_id == 'A1' for b in breaches):
                            breaches.append(AcceptanceBreach('A1', cycle, f"{k}: {v}", f"<= {target}"))
                            
            # Check A2
            if sample.raster_after_exit.resident_bytes > 0:
                if not any(b.criterion_id == 'A2' for b in breaches):
                    breaches.append(AcceptanceBreach('A2', cycle, str(sample.raster_after_exit.resident_bytes), "0"))
                    
            # Check A1b -- the step 4 gate. A1 is satisfied by a cycle that the
            # explicit gc.collect() reclaimed; A1b requires the wrappers to be
            # gone on refcount alone, at the cycle the layout dropped them.
            if cycle > 1:
                for k, v in sample.live_counts_before_gc.items():
                    target = 1 if k == 'AuditView' else 0
                    if v > target:
                        if not any(b.criterion_id == 'A1b' for b in breaches):
                            breaches.append(AcceptanceBreach('A1b', cycle, f"{k}: {v} before gc", f"<= {target}"))

            # Check A3 -- the step 2 gate. Enforced only from step 2 onward:
            # before it there is no max_cached_bytes to compare against
            # (Patch01 section 4).
            if ceiling is not None and sample.raster_at_peak.resident_bytes > ceiling:
                if not any(b.criterion_id == 'A3' for b in breaches):
                    breaches.append(AcceptanceBreach('A3', cycle, str(sample.raster_at_peak.resident_bytes), f"<= {ceiling}"))

            # Check A6
            if sample.gc_uncollectable_count > 0:
                if not any(b.criterion_id == 'A6' for b in breaches):
                    breaches.append(AcceptanceBreach('A6', cycle, str(sample.gc_uncollectable_count), "0"))

        except Exception as e:
            if not isinstance(e, (ProbeUnavailable, GestureUnavailable, DrainTimeout, BaselineMissing)):
                import traceback
                traceback.print_exc()
            raise e
            
    # Compute A4/A5 slopes using least-squares fit
    if cycles >= 60:
        window_samples = samples[50:]
        x = [s.cycle_index for s in window_samples]
        
        y_rss = [s.rss_bytes for s in window_samples]
        sum_x = sum(x)
        sum_y = sum(y_rss)
        sum_xy = sum(xi*yi for xi,yi in zip(x, y_rss))
        sum_x2 = sum(xi**2 for xi in x)
        n = len(window_samples)
        slope_rss = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x**2)
        
        y_py = [s.python_allocated_bytes for s in window_samples]
        sum_y_py = sum(y_py)
        sum_xypy = sum(xi*yi for xi,yi in zip(x, y_py))
        slope_py = (n * sum_xypy - sum_x * sum_y_py) / (n * sum_x2 - sum_x**2)
        
        if slope_rss > 512 * 1024:
            if not any(b.criterion_id == 'A5' for b in breaches):
                breaches.append(AcceptanceBreach('A5', 200, f"{slope_rss/1024:.2f} KB/cycle", "< 512 KB/cycle"))
                
        # threshold for py blocks? approx 64KB (2000 blocks?)
        if slope_py > 65536:
            if not any(b.criterion_id == 'A4' for b in breaches):
                breaches.append(AcceptanceBreach('A4', 200, f"{slope_py} bytes/cycle", "< 65536 bytes/cycle"))
                
    return ChurnReport(
        tree_revision="optimize06",
        probe_implementation=probe_name,
        canvas_viewport_height=window._audit_view._layout_canvas.height(),
        captured_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        cycles_requested=cycles,
        samples=tuple(samples),
        breaches=BreachLedger(tuple(breaches))
    )

def run_gate(cycles: int, window: MainWindow, fixture: ChurnFixture, baseline: ChurnReport) -> Optional[AcceptanceBreach]:
    # Gate implementation will return first breach immediately
    probe = select_raster_probe(window._audit_view._layout_canvas)
    if probe.implementation_name() != baseline.probe_implementation:
        raise BaselineMissing("", baseline.probe_implementation, baseline.canvas_viewport_height)

    ceiling = raster_budget_ceiling(window)
    bind_render_observer(window)

    for cycle in range(1, cycles + 1):
        sample = run_churn_cycle(window, fixture, cycle)

        for k, v in sample.live_counts_after_exit.items():
            target = 1 if k == 'AuditView' else 0
            if v > target:
                return AcceptanceBreach('A1', cycle, f"{k}: {v}", f"<= {target}")

        for k, v in sample.live_counts_before_gc.items():
            target = 1 if k == 'AuditView' else 0
            if v > target:
                return AcceptanceBreach('A1b', cycle, f"{k}: {v} before gc", f"<= {target}")

        if sample.raster_after_exit.resident_bytes > 0:
            return AcceptanceBreach('A2', cycle, str(sample.raster_after_exit.resident_bytes), "0")

        if ceiling is not None and sample.raster_at_peak.resident_bytes > ceiling:
            return AcceptanceBreach('A3', cycle, str(sample.raster_at_peak.resident_bytes), f"<= {ceiling}")

        if sample.gc_uncollectable_count > 0:
            return AcceptanceBreach('A6', cycle, str(sample.gc_uncollectable_count), "0")
            
    return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=200)
    parser.add_argument("--mode", choices=["baseline", "gate"], default="baseline")
    parser.add_argument("--keep-fixture", action="store_true")
    parser.add_argument("--trace-referrers", action="store_true")
    args = parser.parse_args()
    
    if args.trace_referrers:
        try:
            import objgraph
        except ImportError:
            sys.exit("ERROR: objgraph not installed. Cannot trace referrers.")
            
    # A1b reads live counts before an explicit collect. An automatic collection
    # firing between exit-to-list and that read would reclaim a cycle and make
    # the criterion pass on timing. Each cycle still collects explicitly, so
    # nothing accumulates beyond one cycle's worth.
    gc.disable()

    app = QApplication(sys.argv)

    import tempfile
    temp_dir = tempfile.mkdtemp()
    root_path = pathlib.Path(temp_dir)
    
    migration_outcome = migrate_to_versioned_layout(root_path)
    fixture = seed_churn_fixture(migration_outcome.target_root, reference_page_count=8)
    
    config = AppConfig(
        app_data_root=migration_outcome.target_root,
        db_path=migration_outcome.target_root / "local_audit.db",
        file_storage_root=migration_outcome.target_root / "uploads",
        coord_map_path=None,
        log_path=migration_outcome.target_root / "cockpit.log",
        log_level="DEBUG",
        probe_history=()
    )
    
    settings = QSettings(str(config.app_data_root / "settings.ini"), QSettings.Format.IniFormat)
    runtime_settings_controller = RuntimeCalcSettingsController(settings)
    bootstrapped = bootstrap(config, constants_provider=runtime_settings_controller.constants)
    
    ui_dir = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui"
    theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
    style_controller = StyleController(app, theme, settings)
    app.setStyleSheet(style_controller.compose())
    
    observed_args = {
        "theme", "app", "settings", "style_controller", "runtime_settings_controller",
        "bootstrapped_app", "audit_read_svc", "checklist_svc", "split_svc", 
        "completion_svc", "layout_query_svc", "pdf_renderer", "holiday_svc"
    }
    assert_construction_parity(observed_args)
    
    main_window = MainWindow(
        theme=theme,
        app=app,
        settings=settings,
        style_controller=style_controller,
        runtime_settings_controller=runtime_settings_controller,
        bootstrapped_app=bootstrapped,
        audit_read_svc=bootstrapped.audit_read_svc,
        checklist_svc=bootstrapped.checklist_svc,
        split_svc=bootstrapped.split_svc,
        completion_svc=bootstrapped.completion_svc,
        layout_query_svc=bootstrapped.layout_query_svc,
        pdf_renderer=bootstrapped.pdf_renderer,
        holiday_svc=bootstrapped.holiday_svc
    )
    main_window.showMaximized()
    
    app.processEvents()
    time.sleep(1)
    app.processEvents()
    
    if args.mode == "baseline":
        report = run_baseline(args.cycles, main_window, fixture)
        measurements_dir = pathlib.Path(__file__).parent / "measurements"
        measurements_dir.mkdir(exist_ok=True)
        report_file = measurements_dir / f"churn-{report.captured_at_utc.replace(':', '-')}-{report.tree_revision}-{report.probe_implementation}-{report.canvas_viewport_height}px.json"
        report_file.write_text(report.to_json())
        print(f"Baseline completed. {len(report.breaches.entries)} breaches recorded.")
        for b in report.breaches.entries:
            print(f" - {b.criterion_id} at cycle {b.first_cycle}: {b.observed} (target {b.threshold})")
            
        if len(report.breaches.entries) == 0:
            # H3, the positive control, characterizes an UNREMEDIATED tree. On a
            # tree carrying Optimize06 steps 2-6 it is expected to report
            # nothing, and per Patch01 section 13 that is the "tree moved"
            # branch, not a harness defect -- provided H1 and H2 still pass.
            # run_baseline's exit status is 0 unless the harness itself failed
            # (Patch01 section 7.1), so this is a note, not an error.
            print(
                "NOTE: no breaches recorded. Expected on a remediated tree. "
                "If this tree is meant to be pre-remediation, verify H1/H2 "
                "before concluding the defects are gone."
            )
    else:
        # Load latest baseline
        measurements_dir = pathlib.Path(__file__).parent / "measurements"
        baselines = sorted(measurements_dir.glob("churn-*.json"))
        if not baselines:
            raise BaselineMissing(measurements_dir, "any", main_window._audit_view._layout_canvas.height())
        baseline = ChurnReport.from_json(baselines[-1].read_text())
        
        breach = run_gate(args.cycles, main_window, fixture, baseline)
        if breach:
            print(f"GATE FAILED: {breach.criterion_id} at cycle {breach.first_cycle}: {breach.observed}")
            sys.exit(1)
        print("GATE PASSED.")
        
    app.quit()
    
    if not args.keep_fixture:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    main()
