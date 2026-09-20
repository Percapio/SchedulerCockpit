"""Phase 49 section 8.3 — the one-time term-merge notice.

A one-shot at the call site rather than a visibility listener: showEvent would
re-fire on every minimise and restore. The idempotence guard is what makes that
safe even if a caller schedules it twice.
"""

import pytest

from cockpit.services.second_ops import TermMergeOutcome
from cockpit.ui.main_window import MainWindow


class _Recorder:
    def __init__(self):
        self.calls = []

    def show_toast(self, title, subtitle):
        self.calls.append((title, subtitle))


@pytest.fixture
def window():
    win = MainWindow.__new__(MainWindow)
    win._term_merge_announced = False
    win.toast = _Recorder()
    return win


def test_non_empty_outcome_raises_one_toast_naming_the_count(window):
    window.announce_term_merge(TermMergeOutcome(added=("A", "B", "C"), generation_advanced_to=2))

    assert len(window.toast.calls) == 1
    title, subtitle = window.toast.calls[0]
    assert title == "3 new 2nd OPS terms added"
    assert "2nd OPS" in subtitle


def test_empty_outcome_raises_nothing(window):
    window.announce_term_merge(TermMergeOutcome(added=(), generation_advanced_to=2))
    assert window.toast.calls == []


def test_missing_outcome_raises_nothing(window):
    window.announce_term_merge(None)
    assert window.toast.calls == []


def test_second_call_is_inert(window):
    outcome = TermMergeOutcome(added=("A",), generation_advanced_to=2)
    window.announce_term_merge(outcome)
    window.announce_term_merge(outcome)
    assert len(window.toast.calls) == 1


def test_an_empty_first_call_still_consumes_the_one_shot(window):
    """Guards against a second scheduling raising a notice the operator missed."""
    window.announce_term_merge(TermMergeOutcome(added=(), generation_advanced_to=2))
    window.announce_term_merge(TermMergeOutcome(added=("A",), generation_advanced_to=2))
    assert window.toast.calls == []
