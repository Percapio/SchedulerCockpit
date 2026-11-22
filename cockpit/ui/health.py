"""The health line (Patch 14 §10, INV-H1).

One INFO record per reading: "health:" followed by key=value pairs in a fixed order, so a census can read
a week of them. Every field is read in O(widgets + retained threads), never O(Python heap), so the GUI
thread can afford a reading at any time. A provider that fails yields "na" for its fields.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QApplication, QDialog

from cockpit.ui.process_counters import ProcessCounters, read_process_counters

logger = logging.getLogger("cockpit.health")

HEALTH_PREFIX = "health:"
NA = "na"

KEYS: tuple[str, ...] = (
    "at", "up_h", "page", "busy",
    "ws_mb", "priv_mb", "handles", "gdi", "user",
    "py_blocks",
    "widgets", "top", "hidden_dialogs",
    "threads_py",
    "retained", "retained_oldest_s",
    "gc_n0", "gc_n1", "gc_n2", "gc_max0_ms", "gc_max1_ms", "gc_max2_ms", "gc_over", "gc_off", "gc_frozen",
    "db_refused", "db_off_owner",
    "pdf_sections", "pdf_max_hold_ms", "pdf_max_wait_ms",
)


class HealthReason(StrEnum):
    STARTUP = "startup"
    INTERVAL = "interval"
    EXIT = "exit"


@dataclass
class HealthProviders:
    """Where each group of fields comes from. A provider left None reads as "na"."""

    started_at_ms: int
    clock: Callable[[], int]
    counters: Callable[[], ProcessCounters] = read_process_counters
    page: Optional[Callable[[], str]] = None
    busy: Optional[Callable[[], str]] = None
    collector: Any = None
    retention: Any = None
    connection: Any = None
    pdf: Optional[Callable[[], Any]] = None


def _value_or_na(value: object) -> object:
    return NA if value is None else value


def _fill(reading: OrderedDict, keys: tuple[str, ...], read: Callable[[], tuple]) -> None:
    try:
        values = read()
    except Exception:
        logger.debug("health provider for %s failed", keys, exc_info=True)
        values = (NA,) * len(keys)
    for key, value in zip(keys, values):
        reading[key] = _value_or_na(value)


def read_health(providers: HealthProviders, reason: HealthReason) -> "OrderedDict[str, object]":
    """One reading of the process's health, cheap enough for the GUI thread.

    pre:  GUI thread
    post: every key of KEYS, in order. A count with nothing to count is 0, a maximum with no samples is 0,
          retained_oldest_s is 0 when nothing is retained, an absent counter and a missing or failing
          provider are "na"
    raises: nothing
    """
    reading: OrderedDict = OrderedDict((key, NA) for key in KEYS)
    reading["at"] = reason.value

    def uptime() -> tuple:
        return (round((providers.clock() - providers.started_at_ms) / 3_600_000.0, 2),)

    def page() -> tuple:
        return (providers.page() if providers.page is not None else NA,)

    def busy() -> tuple:
        return (providers.busy() if providers.busy is not None else NA,)

    def counters() -> tuple:
        c = providers.counters()
        return (c.working_set_mb, c.private_mb, c.handles, c.gdi_objects, c.user_objects)

    def widgets() -> tuple:
        app = QApplication.instance()
        if app is None:
            return (NA, NA, NA)
        tops = QApplication.topLevelWidgets()
        hidden = sum(1 for w in tops if isinstance(w, QDialog) and not w.isVisible())
        return (len(QApplication.allWidgets()), len(tops), hidden)

    def retention() -> tuple:
        if providers.retention is None:
            return (NA, NA)
        entries = providers.retention.outstanding()
        if not entries:
            return (0, 0)
        now = providers.clock()
        oldest_ms = max(now - entry.retained_at_ms for entry in entries)
        return (len(entries), int(oldest_ms // 1000))

    def collector() -> tuple:
        if providers.collector is None:
            return (NA,) * 9
        r = providers.collector.reading()
        return (
            r.collections.gen0, r.collections.gen1, r.collections.gen2,
            round(r.max_pause_ms.gen0_ms, 1), round(r.max_pause_ms.gen1_ms, 1), round(r.max_pause_ms.gen2_ms, 1),
            r.over_budget, r.off_thread, r.frozen,
        )

    def database() -> tuple:
        counts = getattr(providers.connection, "gate_counts", None)
        if counts is None:
            return (NA, NA)
        return tuple(counts())

    def pdf() -> tuple:
        if providers.pdf is None:
            return (NA, NA, NA)
        r = providers.pdf()
        return (r.sections, round(r.max_hold_ms, 1), round(r.max_wait_ms, 1))

    _fill(reading, ("up_h",), uptime)
    _fill(reading, ("page",), page)
    _fill(reading, ("busy",), busy)
    _fill(reading, ("ws_mb", "priv_mb", "handles", "gdi", "user"), counters)
    _fill(reading, ("py_blocks",), lambda: (sys.getallocatedblocks(),))
    _fill(reading, ("widgets", "top", "hidden_dialogs"), widgets)
    _fill(reading, ("threads_py",), lambda: (threading.active_count(),))
    _fill(reading, ("retained", "retained_oldest_s"), retention)
    _fill(reading, (
        "gc_n0", "gc_n1", "gc_n2", "gc_max0_ms", "gc_max1_ms", "gc_max2_ms", "gc_over", "gc_off", "gc_frozen",
    ), collector)
    _fill(reading, ("db_refused", "db_off_owner"), database)
    _fill(reading, ("pdf_sections", "pdf_max_hold_ms", "pdf_max_wait_ms"), pdf)
    return reading


def format_health(reading: "OrderedDict[str, object]") -> str:
    """The reading as one line: "health:" and key=value pairs in KEYS order."""
    return HEALTH_PREFIX + " " + " ".join(f"{key}={reading[key]}" for key in KEYS)


class HealthLogger(QObject):
    """Logs a health line now, and every interval after start() (Patch 14 §10.2)."""

    def __init__(self, providers: HealthProviders, interval_ms: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.providers = providers
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._on_interval)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def log_now(self, reason: HealthReason) -> str:
        """Logs one reading.

        pre:  GUI thread
        post: one INFO record holding format_health's line; the line is returned
        raises: nothing
        """
        try:
            line = format_health(read_health(self.providers, reason))
        except Exception:
            logger.exception("health reading failed")
            return ""
        logger.info("%s", line)
        return line

    def _on_interval(self) -> None:
        self.log_now(HealthReason.INTERVAL)
