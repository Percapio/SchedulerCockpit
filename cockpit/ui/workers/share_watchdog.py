"""The watchdog on share calls (Phase 55 §7, amended 2026-11-14; Phase 55b §4.1).

The watchdog runs exactly while a call that reads the share is in progress: a worker routes each
share call through its guard, and the UI thread starts its single-shot timer on beacon.started and
stops it on beacon.finished. Local work, however slow, never trips it.
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

from PyQt6.QtCore import QObject, pyqtSignal

T = TypeVar("T")


class ShareCallBeacon(QObject):
    started = pyqtSignal()
    finished = pyqtSignal()


def within_share_watchdog(call: Callable[[], T], beacon: ShareCallBeacon) -> T:
    """Runs one blocking share call under the UI thread's watchdog.

    pre:  called on the worker's thread
    post: beacon.started is emitted before call, and beacon.finished after it returns or raises;
          both cross to the UI thread in the order they were emitted
    raises: whatever call raises
    """
    beacon.started.emit()
    try:
        return call()
    finally:
        beacon.finished.emit()


class _DepthCountingGuard:
    """A ShareCallGuard that counts nesting per thread (Phase 55b §4.1).

    started is emitted when the depth goes from 0 to 1 and finished when it returns to 0, so a guarded
    call made inside another can't stop the watchdog while the outer call still waits on the share.
    """

    def __init__(self, beacon: ShareCallBeacon) -> None:
        self._beacon = beacon
        self._local = threading.local()

    def __call__(self, call: Callable[[], T]) -> T:
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        if depth == 0:
            self._beacon.started.emit()
        try:
            return call()
        finally:
            self._local.depth = depth
            if depth == 0:
                self._beacon.finished.emit()


def share_call_guard(beacon: ShareCallBeacon) -> Callable[[Callable[[], T]], T]:
    """The ShareCallGuard a worker hands to the services: every share call under the watchdog."""
    return _DepthCountingGuard(beacon)
