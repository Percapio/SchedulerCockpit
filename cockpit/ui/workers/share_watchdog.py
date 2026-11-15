"""The watchdog on share calls (Phase 55 §7, amended 2026-11-14).

The watchdog runs exactly while a call that reads the share is in progress: a worker routes each
share call through within_share_watchdog, and the UI thread starts its single-shot timer on
beacon.started and stops it on beacon.finished. Local work, however slow, never trips it.
"""

from __future__ import annotations

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


def share_call_guard(beacon: ShareCallBeacon) -> Callable[[Callable[[], T]], T]:
    """The ShareCallGuard a worker hands to the services: every share call under the watchdog."""
    return lambda call: within_share_watchdog(call, beacon)
