"""Patch 11 sections 6 and 8.5 — abandoned fetch threads must actually end.

The discard assertion is worthless on its own: a list nothing is ever added
back to satisfies "is empty" trivially. Each test here pairs the discard with
the condition that has to hold for it to mean anything.
"""

import threading
import time

import pytest
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from cockpit.ui.main_window import MainWindow


class _GatedFetchWorker(QObject):
    """Stands in for JobFetchWorker: one blocking call, then a signal."""

    succeeded_signal = pyqtSignal(object)
    failed_signal = pyqtSignal(object)

    def __init__(self, release: threading.Event, entered: threading.Event):
        super().__init__()
        self._release = release
        self._entered = entered

    def run(self):
        self._entered.set()
        self._release.wait(timeout=10)
        self.succeeded_signal.emit(object())


class _WindowStub:
    """Carries exactly the attributes _abandon_fetch_worker touches.

    Building a real MainWindow would drag in the whole bootstrap for a method
    that reads six attributes; the method under test is the real one, bound
    here.
    """

    def __init__(self, qtbot):
        self._operation_in_flight = True
        self._orphaned_workers = []
        self._fetch_watchdog = QTimer()
        self._fetch_thread = None
        self._fetch_worker = None

    def _on_fetch_succeeded(self, outcome):  # pragma: no cover - must never run
        raise AssertionError("an abandoned fetch delivered its result")

    def _on_fetch_failed(self, payload):  # pragma: no cover - must never run
        raise AssertionError("an abandoned fetch delivered its failure")

    _abandon_fetch_worker = MainWindow._abandon_fetch_worker
    _discard_orphan = MainWindow._discard_orphan


@pytest.fixture
def gated_fetch(qtbot):
    """Yields the stub, a liveness probe, and the release gate.

    The thread object is deliberately NOT yielded: abandonment wires
    deleteLater to finished, so the Python wrapper goes stale the moment the
    thread ends and touching it raises. Liveness is read through a flag set by
    a signal instead, which is what a caller could actually observe.
    """
    release, entered = threading.Event(), threading.Event()
    window = _WindowStub(qtbot)

    thread = QThread()
    worker = _GatedFetchWorker(release, entered)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.succeeded_signal.connect(window._on_fetch_succeeded)
    worker.failed_signal.connect(window._on_fetch_failed)

    ended = []
    thread.finished.connect(lambda: ended.append(True))

    window._fetch_thread = thread
    window._fetch_worker = worker
    thread.start()
    qtbot.waitUntil(entered.is_set, timeout=5000)

    yield window, ended, release

    release.set()
    qtbot.wait(200)


def test_quit_reaches_a_worker_that_is_still_blocked(gated_fetch, qtbot):
    """The assertion that fails against the obvious, wrong fix.

    QThread.run() is an event loop. Before this patch the abandon path
    disconnected the only handlers that called quit() and called nothing in
    their place, so finished never fired and the thread ran forever. A fix that
    only added a discard-on-finished would have changed nothing.
    """
    window, ended, release = gated_fetch

    window._abandon_fetch_worker()
    assert not ended, "the worker is still inside its blocking call"

    release.set()
    qtbot.waitUntil(lambda: bool(ended), timeout=5000)


def test_the_list_discards_once_the_thread_ends(gated_fetch, qtbot):
    window, ended, release = gated_fetch

    window._abandon_fetch_worker()
    assert len(window._orphaned_workers) == 1, "retained while it is still running"

    release.set()
    qtbot.waitUntil(lambda: not window._orphaned_workers, timeout=5000)


def test_a_worker_that_never_returns_stays_retained(gated_fetch, qtbot):
    """Section 7.2: this is a deliberate outcome, not an oversight.

    Dropping the reference to a running QThread is the abort the whole patch
    exists to close, so a test demanding an empty list here would be demanding
    the crash.
    """
    window, ended, release = gated_fetch

    window._abandon_fetch_worker()
    qtbot.wait(300)

    assert window._orphaned_workers, "a stuck thread must stay referenced"
    assert not ended, "and it must still be running"


def test_abandonment_does_not_block_on_the_share(gated_fetch, qtbot):
    """quit() is not wait(). This is what stops anyone 'improving' it into one."""
    window, ended, release = gated_fetch

    started_at = time.monotonic()
    window._abandon_fetch_worker()
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.0
    assert window._operation_in_flight is False


def test_an_abandoned_fetch_never_delivers(gated_fetch, qtbot):
    """_WindowStub's handlers raise, so a delivery fails this test loudly."""
    window, ended, release = gated_fetch

    window._abandon_fetch_worker()
    release.set()
    qtbot.waitUntil(lambda: bool(ended), timeout=5000)
    qtbot.wait(100)
