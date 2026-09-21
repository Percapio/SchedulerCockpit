"""Patch 11 section 8.2 — the bounded probe seam.

Every test here exists because its absence let a specific abort or leak ship:
a handler reaching a destroyed widget, a QThread collected while running, an
exception on a worker thread, and a retention registry that never discards.
"""

import logging
import threading

import pytest
from PyQt6.QtCore import QObject, QThread
from PyQt6.QtWidgets import QWidget

from cockpit.ui.widgets.qt_lifecycle import (
    _ADMISSIONS,
    _IN_FLIGHT_PROBES,
    start_bounded_probe,
)

WATCHDOG_MS = 30_000  # long enough never to fire unless a test wants it


@pytest.fixture(autouse=True)
def _clean_registries():
    _IN_FLIGHT_PROBES.clear()
    _ADMISSIONS.clear()
    yield
    _IN_FLIGHT_PROBES.clear()
    _ADMISSIONS.clear()


class _Gate:
    """A work callable that blocks until released, like a stat on a dead share."""

    def __init__(self, outcome="done"):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._outcome = outcome

    def __call__(self):
        self.entered.set()
        self.release.wait(timeout=10)
        return self._outcome


def _drain(qtbot):
    qtbot.waitUntil(lambda: not _IN_FLIGHT_PROBES, timeout=5000)


def test_the_outcome_is_delivered_on_the_ui_thread(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    ui_thread = QThread.currentThread()

    seen, threads = [], []
    start_bounded_probe(
        owner=owner,
        work=lambda: "reachable",
        watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: (seen.append(o), threads.append(QThread.currentThread())),
        on_timeout=lambda: seen.append("TIMEOUT"),
        on_error=lambda e: seen.append(e),
    )

    qtbot.waitUntil(lambda: bool(seen), timeout=5000)
    assert seen == ["reachable"]
    assert threads[0] is ui_thread
    _drain(qtbot)


def test_work_runs_off_the_ui_thread(qtbot):
    """A future caller must not be able to quietly pass widget-touching work."""
    owner = QWidget()
    qtbot.addWidget(owner)
    ui_thread = QThread.currentThread()

    ran_on = []
    start_bounded_probe(
        owner=owner,
        work=lambda: ran_on.append(QThread.currentThread()),
        watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: None,
        on_timeout=lambda: None,
        on_error=lambda e: None,
    )

    qtbot.waitUntil(lambda: bool(ran_on), timeout=5000)
    assert ran_on[0] is not ui_thread
    _drain(qtbot)


# --- section 4.5: two threads, at most one delivery -------------------------

def test_superseding_severs_the_first_while_it_is_genuinely_in_flight(qtbot):
    """Deliberately NOT written as 'time the first out, then launch a second'.

    That variant passes against a seam with no superseding at all, which is how
    an earlier draft mistook a call-site property for a seam invariant. Here the
    first admission is still running when the second launches.
    """
    owner = QWidget()
    qtbot.addWidget(owner)

    first, second = _Gate("first"), _Gate("second")
    delivered = []

    start_bounded_probe(
        owner=owner, work=first, watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: delivered.append(o),
        on_timeout=lambda: delivered.append("TIMEOUT-1"),
        on_error=lambda e: delivered.append(e),
    )
    qtbot.waitUntil(first.entered.is_set, timeout=5000)

    # The first is blocked, not timed out, not abandoned by the caller.
    start_bounded_probe(
        owner=owner, work=second, watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: delivered.append(o),
        on_timeout=lambda: delivered.append("TIMEOUT-2"),
        on_error=lambda e: delivered.append(e),
    )
    qtbot.waitUntil(second.entered.is_set, timeout=5000)

    assert len(_IN_FLIGHT_PROBES) == 2, "both threads must stay retained"

    second.release.set()
    qtbot.waitUntil(lambda: bool(delivered), timeout=5000)

    first.release.set()
    _drain(qtbot)

    assert delivered == ["second"], "the superseded admission must not deliver"


def test_superseding_is_per_owner(qtbot):
    owner_a, owner_b = QWidget(), QWidget()
    qtbot.addWidget(owner_a)
    qtbot.addWidget(owner_b)

    gate_a, gate_b = _Gate("a"), _Gate("b")
    delivered = []

    for owner, gate in ((owner_a, gate_a), (owner_b, gate_b)):
        start_bounded_probe(
            owner=owner, work=gate, watchdog_ms=WATCHDOG_MS,
            on_outcome=lambda o: delivered.append(o),
            on_timeout=lambda: delivered.append("TIMEOUT"),
            on_error=lambda e: delivered.append(e),
        )

    qtbot.waitUntil(lambda: gate_a.entered.is_set() and gate_b.entered.is_set(), timeout=5000)
    gate_a.release.set()
    gate_b.release.set()
    qtbot.waitUntil(lambda: len(delivered) == 2, timeout=5000)

    assert sorted(delivered) == ["a", "b"]
    _drain(qtbot)


# --- section 4.2: on_error ---------------------------------------------------

def test_work_that_raises_reaches_on_error_and_nothing_aborts(qtbot, caplog):
    owner = QWidget()
    qtbot.addWidget(owner)

    def exploding_work():
        raise ValueError("the probe is broken, not the share")

    outcomes, timeouts, errors = [], [], []
    with caplog.at_level(logging.ERROR, logger="cockpit.ui.widgets.qt_lifecycle"):
        start_bounded_probe(
            owner=owner, work=exploding_work, watchdog_ms=WATCHDOG_MS,
            on_outcome=outcomes.append,
            on_timeout=lambda: timeouts.append(True),
            on_error=errors.append,
        )
        qtbot.waitUntil(lambda: bool(errors), timeout=5000)

    assert isinstance(errors[0], ValueError)
    assert outcomes == [] and timeouts == []
    assert any("bounded probe work raised" in r.getMessage() for r in caplog.records)
    _drain(qtbot)


# --- section 4.3: the thread always exits ------------------------------------

def test_the_thread_exits_after_a_normal_return(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    start_bounded_probe(
        owner=owner, work=lambda: "ok", watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: None, on_timeout=lambda: None, on_error=lambda e: None,
    )
    _drain(qtbot)
    assert _IN_FLIGHT_PROBES == set()
    assert _ADMISSIONS == set()


def test_the_thread_exits_after_work_raises(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)

    def exploding_work():
        raise RuntimeError("boom")

    start_bounded_probe(
        owner=owner, work=exploding_work, watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: None, on_timeout=lambda: None, on_error=lambda e: None,
    )
    _drain(qtbot)
    assert _IN_FLIGHT_PROBES == set()


def test_the_thread_exits_after_abandon_once_the_work_returns(qtbot):
    """The case _abandon_fetch_worker got wrong.

    Abandonment severs the caller's handler; if it also severed the wiring that
    ends the thread, the thread would spin in exec() forever and the registry
    would never discard. That is a leak dressed as a fix.
    """
    owner = QWidget()
    qtbot.addWidget(owner)
    gate = _Gate()
    delivered = []

    admission = start_bounded_probe(
        owner=owner, work=gate, watchdog_ms=WATCHDOG_MS,
        on_outcome=delivered.append,
        on_timeout=lambda: delivered.append("TIMEOUT"),
        on_error=delivered.append,
    )
    qtbot.waitUntil(gate.entered.is_set, timeout=5000)

    admission.abandon()
    assert admission.is_in_flight() is False

    gate.release.set()
    _drain(qtbot)

    assert delivered == [], "an abandoned admission must not deliver"
    assert _IN_FLIGHT_PROBES == set(), "the thread must still have exited"


def test_abandon_is_idempotent_and_safe_after_settlement(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    delivered = []

    admission = start_bounded_probe(
        owner=owner, work=lambda: "ok", watchdog_ms=WATCHDOG_MS,
        on_outcome=delivered.append, on_timeout=lambda: None, on_error=lambda e: None,
    )
    qtbot.waitUntil(lambda: bool(delivered), timeout=5000)

    admission.abandon()
    admission.abandon()
    _drain(qtbot)
    assert delivered == ["ok"]


def test_the_watchdog_severs_and_reports(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    gate = _Gate()
    outcomes, timeouts = [], []

    start_bounded_probe(
        owner=owner, work=gate, watchdog_ms=120,
        on_outcome=outcomes.append,
        on_timeout=lambda: timeouts.append(True),
        on_error=lambda e: None,
    )
    qtbot.waitUntil(lambda: bool(timeouts), timeout=5000)

    # The work is still running. Its late result must be discarded, not shown.
    gate.release.set()
    _drain(qtbot)
    assert outcomes == []


def test_a_destroyed_owner_cannot_be_reached_by_a_late_result(qtbot):
    """The abort this patch exists to close, asserted directly.

    Reaching a deleted widget here would terminate the process rather than fail
    the assertion, so a pass is the whole point.
    """
    owner = QWidget()
    gate = _Gate()
    delivered = []

    start_bounded_probe(
        owner=owner, work=gate, watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: delivered.append(owner.objectName()),
        on_timeout=lambda: delivered.append("TIMEOUT"),
        on_error=lambda e: delivered.append(e),
    )
    qtbot.waitUntil(gate.entered.is_set, timeout=5000)

    owner.deleteLater()
    owner.destroy()
    qtbot.wait(50)

    gate.release.set()
    _drain(qtbot)
    assert delivered == []


def test_a_second_launch_never_drops_the_first_thread(qtbot):
    """Section 2.3: rebinding a single slot collected a running QThread.

    The registry is what makes a second launch survivable, so it is asserted on
    the thread objects themselves rather than on a count alone.
    """
    owner = QWidget()
    qtbot.addWidget(owner)
    first, second = _Gate("1"), _Gate("2")

    a = start_bounded_probe(
        owner=owner, work=first, watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: None, on_timeout=lambda: None, on_error=lambda e: None,
    )
    qtbot.waitUntil(first.entered.is_set, timeout=5000)
    b = start_bounded_probe(
        owner=owner, work=second, watchdog_ms=WATCHDOG_MS,
        on_outcome=lambda o: None, on_timeout=lambda: None, on_error=lambda e: None,
    )
    qtbot.waitUntil(second.entered.is_set, timeout=5000)

    assert a.thread is not b.thread
    assert a.thread.isRunning() and b.thread.isRunning()
    assert {id(t) for t, _ in _IN_FLIGHT_PROBES} == {id(a.thread), id(b.thread)}

    first.release.set()
    second.release.set()
    _drain(qtbot)
