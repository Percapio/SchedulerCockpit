"""The cyclic collector, run only on the GUI thread (Patch 14 §6, INV-T1).

CPython collects on whichever thread's allocation crosses a threshold, QThread workers included, and a
collection frees every Qt object in the cyclic garbage on that thread. Automatic collection is therefore
turned off, and a GUI-thread timer collects instead, choosing generations as CPython's own thresholds
would, except that a full collection waits for idle or for an interval.

The budgets are alert thresholds, not bounds: a collection can't be interrupted, so they only decide what
is logged and counted. The gc callback records and never logs; the tick logs afterwards.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Optional, Protocol

from PyQt6.QtCore import QTimer

logger = logging.getLogger(__name__)


class CollectionTrigger(StrEnum):
    YOUNG = "young"
    MIDDLE = "middle"
    FULL_IDLE = "full_idle"
    FULL_FORCED = "full_forced"
    FREEZE = "freeze"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class CollectorSettings:
    tick_ms: int = 100
    young_threshold: int = 2_000
    middle_threshold: int = 10
    full_interval_ms: int = 1_800_000
    young_budget_ms: int = 50
    full_budget_ms: int = 150

    @classmethod
    def from_config(cls, config) -> "CollectorSettings":
        return cls(
            tick_ms=config.gc_tick_ms,
            young_threshold=config.gc_young_threshold,
            middle_threshold=config.gc_middle_threshold,
            full_interval_ms=config.gc_full_interval_ms,
            young_budget_ms=config.gc_young_budget_ms,
            full_budget_ms=config.gc_full_budget_ms,
        )


@dataclass(frozen=True)
class GcCounts:
    young: int
    middle: int
    old: int


@dataclass
class GenerationCounts:
    gen0: int = 0
    gen1: int = 0
    gen2: int = 0


@dataclass
class GenerationDurations:
    gen0_ms: float = 0.0
    gen1_ms: float = 0.0
    gen2_ms: float = 0.0


@dataclass(frozen=True)
class CollectorReading:
    collections: GenerationCounts
    max_pause_ms: GenerationDurations
    over_budget: int
    off_thread: int
    frozen: int


@dataclass(frozen=True)
class OverBudget:
    generation: int
    duration_ms: float
    trigger: CollectionTrigger


class GcPort(Protocol):
    def collect(self, generation: int) -> int: ...
    def counts(self) -> GcCounts: ...
    def freeze(self) -> None: ...
    def unfreeze(self) -> None: ...
    def freeze_count(self) -> int: ...
    def enable(self) -> None: ...
    def disable(self) -> None: ...
    def is_enabled(self) -> bool: ...
    def add_callback(self, callback: Callable[[str, dict], None]) -> None: ...
    def remove_callback(self, callback: Callable[[str, dict], None]) -> None: ...


class PythonGc:
    """The gc module, through GcPort."""

    def collect(self, generation: int) -> int:
        return gc.collect(generation)

    def counts(self) -> GcCounts:
        young, middle, old = gc.get_count()
        return GcCounts(young, middle, old)

    def freeze(self) -> None:
        gc.freeze()

    def unfreeze(self) -> None:
        gc.unfreeze()

    def freeze_count(self) -> int:
        return gc.get_freeze_count()

    def enable(self) -> None:
        gc.enable()

    def disable(self) -> None:
        gc.disable()

    def is_enabled(self) -> bool:
        return gc.isenabled()

    def add_callback(self, callback: Callable[[str, dict], None]) -> None:
        gc.callbacks.append(callback)

    def remove_callback(self, callback: Callable[[str, dict], None]) -> None:
        try:
            gc.callbacks.remove(callback)
        except ValueError:
            pass


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass
class CollectorState:
    gui_ident: int
    last_full_ms: int
    full_requested: bool = False
    trigger: Optional[CollectionTrigger] = None
    started: dict[int, float] = field(default_factory=dict)
    counts: GenerationCounts = field(default_factory=GenerationCounts)
    max_pause_ms: GenerationDurations = field(default_factory=GenerationDurations)
    over_budget: list[OverBudget] = field(default_factory=list)
    over_budget_count: int = 0
    long_quiet: list[OverBudget] = field(default_factory=list)
    off_thread_count: int = 0
    off_thread_seen: set[int] = field(default_factory=set)
    off_thread_unlogged: list[int] = field(default_factory=list)


class CollectorHandle:
    """The GUI-thread collector: its timer, its gc callback and its readings."""

    def __init__(
        self,
        settings: CollectorSettings,
        gc_port: GcPort,
        clock: Callable[[], int] = monotonic_ms,
        perf: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.settings = settings
        self._gc = gc_port
        self._clock = clock
        self._perf = perf
        self._busy: Callable[[], str] = lambda: "unknown"
        self.state = CollectorState(gui_ident=threading.get_ident(), last_full_ms=clock())
        self._was_enabled = gc_port.is_enabled()
        self._frozen = False
        self._frozen_by_this_handle = False
        self._disposed = False
        self._timer: Optional[QTimer] = None

    def install(self) -> "CollectorHandle":
        self._gc.disable()
        self._gc.add_callback(self._on_phase)
        self._timer = QTimer()
        self._timer.setInterval(self.settings.tick_ms)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        return self

    def set_busy_provider(self, busy: Callable[[], str]) -> None:
        """Names what the over-budget lines report as busy; MainWindow sets it after construction."""
        self._busy = busy

    def request_full(self) -> None:
        """Asks for a full collection at the next tick (idle maintenance calls it)."""
        self.state.full_requested = True

    def tick(self) -> None:
        """One collector tick: at most one collection, then the lines the gc callback deferred.

        pre:  GUI thread; automatic collection is off
        post: generation 2 when a full collection was requested (FULL_IDLE) or full_interval_ms has passed
              since the last one (FULL_FORCED); otherwise generation 1 when middle >= middle_threshold
              (MIDDLE); otherwise generation 0 when young >= young_threshold (YOUNG); otherwise nothing.
              state.trigger names the collection while it runs
        """
        state = self.state
        counts = self._gc.counts()
        now = self._clock()
        generation: Optional[int] = None
        trigger: Optional[CollectionTrigger] = None
        if state.full_requested:
            generation, trigger = 2, CollectionTrigger.FULL_IDLE
        elif now - state.last_full_ms >= self.settings.full_interval_ms:
            generation, trigger = 2, CollectionTrigger.FULL_FORCED
        elif counts.middle >= self.settings.middle_threshold:
            generation, trigger = 1, CollectionTrigger.MIDDLE
        elif counts.young >= self.settings.young_threshold:
            generation, trigger = 0, CollectionTrigger.YOUNG
        if generation is not None:
            state.trigger = trigger
            try:
                self._gc.collect(generation)
            finally:
                state.trigger = None
            if generation == 2:
                state.full_requested = False
                state.last_full_ms = now
        self.emit_deferred_lines()

    def freeze_startup_heap(self) -> None:
        """Moves everything alive after startup out of the collector's reach (Patch 14 §6.3).

        pre:  GUI thread; called once from the event loop's first turn
        post: one full collection ran (FREEZE), then gc.freeze(); later calls change nothing
        """
        if self._frozen:
            return
        self._frozen = True
        self.state.trigger = CollectionTrigger.FREEZE
        try:
            self._gc.collect(2)
        finally:
            self.state.trigger = None
        self.state.last_full_ms = self._clock()
        self._gc.freeze()
        self._frozen_by_this_handle = True
        logger.info("Collector: froze %d startup objects", self._gc.freeze_count())
        self.emit_deferred_lines()

    def reading(self) -> CollectorReading:
        """The counts and maxima since the previous call, and the totals since launch."""
        state = self.state
        reading = CollectorReading(
            collections=state.counts,
            max_pause_ms=state.max_pause_ms,
            over_budget=state.over_budget_count,
            off_thread=state.off_thread_count,
            frozen=self._gc.freeze_count(),
        )
        state.counts = GenerationCounts()
        state.max_pause_ms = GenerationDurations()
        state.over_budget_count = 0
        return reading

    def dispose(self) -> None:
        """Undoes install(): timer stopped, callback removed, heap unfrozen, collection as it was."""
        if self._disposed:
            return
        self._disposed = True
        if self._timer is not None:
            self._timer.stop()
        self._gc.remove_callback(self._on_phase)
        if self._frozen_by_this_handle:
            self._gc.unfreeze()
        if self._was_enabled:
            self._gc.enable()

    def emit_deferred_lines(self) -> None:
        """Logs what the gc callback recorded but mustn't log itself. GUI thread only."""
        state = self.state
        if state.over_budget:
            busy = self._busy()
            for record in state.over_budget:
                logger.warning(
                    "Collector: generation %d took %.1f ms (%s; over its budget) busy=%s",
                    record.generation, record.duration_ms, record.trigger.value, busy,
                )
            state.over_budget.clear()
        for record in state.long_quiet:
            logger.info(
                "Collector: generation %d took %.1f ms (%s)", record.generation, record.duration_ms,
                record.trigger.value,
            )
        state.long_quiet.clear()
        if state.off_thread_unlogged:
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            for ident in state.off_thread_unlogged:
                logger.warning(
                    "Collector: a collection ran on thread %s (ident %d) while automatic collection is off",
                    names.get(ident, "unknown"), ident,
                )
            state.off_thread_unlogged.clear()

    def _on_phase(self, phase: str, info: dict) -> None:
        """gc.callbacks: records each collection's duration and thread; never logs, formats or locks."""
        state = self.state
        ident = threading.get_ident()
        if phase == "start":
            state.started[ident] = self._perf()
            return
        if phase != "stop":
            return
        started = state.started.pop(ident, None)
        if started is None:
            return
        if ident != state.gui_ident:
            state.off_thread_count += 1
            if ident not in state.off_thread_seen:
                state.off_thread_seen.add(ident)
                state.off_thread_unlogged.append(ident)
            return
        duration_ms = (self._perf() - started) * 1000.0
        generation = info.get("generation", 2)
        trigger = state.trigger or CollectionTrigger.EXPLICIT
        if generation == 0:
            state.counts.gen0 += 1
            state.max_pause_ms.gen0_ms = max(state.max_pause_ms.gen0_ms, duration_ms)
        elif generation == 1:
            state.counts.gen1 += 1
            state.max_pause_ms.gen1_ms = max(state.max_pause_ms.gen1_ms, duration_ms)
        else:
            state.counts.gen2 += 1
            state.max_pause_ms.gen2_ms = max(state.max_pause_ms.gen2_ms, duration_ms)
        if trigger in (CollectionTrigger.YOUNG, CollectionTrigger.MIDDLE):
            if duration_ms > self.settings.young_budget_ms:
                state.over_budget_count += 1
                state.over_budget.append(OverBudget(generation, duration_ms, trigger))
        elif trigger is CollectionTrigger.FULL_FORCED:
            if duration_ms > self.settings.full_budget_ms:
                state.over_budget_count += 1
                state.over_budget.append(OverBudget(generation, duration_ms, trigger))
        elif trigger in (CollectionTrigger.FULL_IDLE, CollectionTrigger.FREEZE):
            if duration_ms > self.settings.full_budget_ms:
                state.long_quiet.append(OverBudget(generation, duration_ms, trigger))


def take_collection_onto_gui_thread(
    settings: CollectorSettings,
    gc_port: Optional[GcPort] = None,
    clock: Callable[[], int] = monotonic_ms,
) -> CollectorHandle:
    """Turns automatic collection off and drives the collector from a GUI-thread timer (INV-T1).

    pre:  called once, on the GUI thread, after QApplication exists and before MainWindow starts a thread
    post: gc is disabled; the first forced full collection comes a full interval after now; the tick runs
          every settings.tick_ms; the callback is registered in gc.callbacks
    raises: nothing
    """
    return CollectorHandle(settings, gc_port or PythonGc(), clock).install()
