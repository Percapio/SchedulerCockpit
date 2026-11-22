"""One registry for every thread that can outlive the object that started it (Patch 14 §5, INV-R1).

Deleting a running QThread from C++ (deleteLater, or a parent's destruction) aborts the process ("QThread:
Destroyed while thread is still running"). Measured on PyQt6 6.11: dropping the last Python reference to a
running QThread does not, because PyQt keeps it alive until it finishes. Five ad-hoc sets used to hold such
threads; this registry replaces them, makes each thread's lifetime explicit, and gives the health line, the
exit line and the "check parked" state one place to read.

It lives outside cockpit.ui because services (the requirements reconciler) retain threads too.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

logger = logging.getLogger(__name__)


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


class ThreadLabel(StrEnum):
    RENDER = "render"
    RECONCILER = "reconciler"
    INGEST = "ingest"
    JOB_FETCH = "job_fetch"
    DRAWING_FETCH = "drawing_fetch"
    DRAWING_UPDATE = "drawing_update"
    UPDATE_CHECK = "update_check"
    PROBE = "probe"
    SECOND_OPS_READ = "second_ops_read"
    CLEANING_REPORT_READ = "cleaning_report_read"
    TRIAL_SCAN = "trial_scan"
    ENRICHMENT = "enrichment"


@dataclass
class RetainedThread:
    label: ThreadLabel
    retained_at_ms: int
    thread: QThread
    worker: Optional[QObject]
    finished_on_arrival: bool = False


class _FinishRelay(QObject):
    """Carries one thread's finished signal to the registry on the GUI thread.

    It is the registry's child, so it lives on the GUI thread and the connection from a worker thread's
    finished is queued there; a bound method of a QObject is delivered in its receiver's thread.
    """

    def __init__(self, registry: "ThreadRetention", key: int) -> None:
        super().__init__(registry)
        self._registry = registry
        self._key = key

    def fire(self) -> None:
        self._registry._release(self._key)


class ThreadRetention(QObject):
    """Holds threads, and their workers, from start until they finish."""

    released = pyqtSignal(str)  # the ThreadLabel's value

    def __init__(self, clock: Callable[[], int] = monotonic_ms, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._clock = clock
        self._held: dict[int, tuple[RetainedThread, _FinishRelay]] = {}

    def retain(self, thread: QThread, worker: Optional[QObject], label: ThreadLabel) -> RetainedThread:
        """Holds a thread, and its worker, from start until the thread finishes (INV-R1).

        pre:  GUI thread
        post: returns the entry. It is in outstanding() until thread.finished is delivered on the GUI
              thread; then both references are dropped and released(label) is emitted. Discarding is
              idempotent. The finished connection is made before the isFinished() check, so a thread that
              has already finished yields an entry that was never held (finished_on_arrival), and released
              is emitted at once. Nothing is stopped, waited on or deleted here. A thread already held
              returns its existing entry unchanged, and a different label or worker is logged at WARNING
        raises: nothing
        """
        key = id(thread)
        held = self._held.get(key)
        if held is not None:
            entry = held[0]
            if entry.label != label or entry.worker is not worker:
                logger.warning(
                    "Thread already retained as %s; a second retain as %s was ignored", entry.label, label,
                )
            return entry
        entry = RetainedThread(label=ThreadLabel(label), retained_at_ms=self._clock(), thread=thread, worker=worker)
        relay = _FinishRelay(self, key)
        self._held[key] = (entry, relay)
        thread.finished.connect(relay.fire)
        if thread.isFinished():
            entry.finished_on_arrival = True
            self._release(key)
        return entry

    def outstanding(self, label: Optional[ThreadLabel] = None) -> list[RetainedThread]:
        """Threads still held, oldest first, optionally only those with one label."""
        entries = [entry for entry, _ in self._held.values() if label is None or entry.label == label]
        return sorted(entries, key=lambda entry: entry.retained_at_ms)

    def describe_outstanding(self) -> str:
        """For the exit line: each retained thread's label and age, or "none"."""
        now = self._clock()
        entries = self.outstanding()
        if not entries:
            return "none"
        return ", ".join(f"{entry.label.value} ({(now - entry.retained_at_ms) // 1000} s)" for entry in entries)

    def _release(self, key: int) -> None:
        held = self._held.pop(key, None)
        if held is None:
            return
        entry, relay = held
        relay.deleteLater()
        self.released.emit(entry.label.value)


_INSTALLED: Optional[ThreadRetention] = None


def thread_retention() -> ThreadRetention:
    """The process's registry: the one installed last, or else the one created on the first call.

    pre:  the first call is on the GUI thread, so the registry lives there
    """
    global _INSTALLED
    if _INSTALLED is None:
        _INSTALLED = ThreadRetention()
    return _INSTALLED


def install_thread_retention(registry: ThreadRetention) -> Optional[ThreadRetention]:
    """Replaces the process's registry, for tests; returns the previous one."""
    global _INSTALLED
    previous = _INSTALLED
    _INSTALLED = registry
    return previous
