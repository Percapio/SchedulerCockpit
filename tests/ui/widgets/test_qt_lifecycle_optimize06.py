"""purge_widget_subtree against its specified contract (Optimize06 section 6,
step 6 gate).

F5: the shipped helper diverged from what Optimize02 R05 specified. Two of the
three divergences are closed here -- the root was never disconnected, and the
"nothing was connected" case was logged as a full traceback. The third,
deferred-versus-synchronous destruction, is a decision recorded in
schedule_destruction() rather than a behaviour change; test_schedule_destruction_
is_deferred_not_synchronous pins which side of it the tree is on, so a future
switch to sip.delete() is a deliberate edit and not a silent one.
"""

import logging

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget
from PyQt6 import sip

from cockpit.ui.widgets.qt_lifecycle import (
    disconnect_all_signals_quietly,
    is_destroyed,
    post_order_including_root,
    purge_widget_subtree,
    schedule_destruction,
)


class Emitter(QWidget):
    fired = pyqtSignal()


def build_tree(parent: QWidget | None = None) -> tuple[Emitter, Emitter, QLabel]:
    """root -> child -> grandchild, each a widget, root carrying a signal."""
    root = Emitter(parent)
    layout = QVBoxLayout(root)
    child = Emitter(root)
    layout.addWidget(child)
    grandchild = QLabel("leaf", child)
    return root, child, grandchild


# ---------------------------------------------------------------------------
# post_order_including_root
# ---------------------------------------------------------------------------

def test_post_order_visits_descendants_before_their_parent(qtbot):
    root, child, grandchild = build_tree()
    qtbot.addWidget(root)

    ordered = post_order_including_root(root)

    assert ordered.index(grandchild) < ordered.index(child)
    assert ordered.index(child) < ordered.index(root)


def test_post_order_includes_root_as_the_final_element(qtbot):
    """The defect: the predecessor omitted root, so the disconnect loop covered
    every node except the one bound to the long-lived parent view."""
    root, _, _ = build_tree()
    qtbot.addWidget(root)

    ordered = post_order_including_root(root)

    assert ordered[-1] is root
    assert root in ordered


def test_post_order_visits_each_widget_exactly_once(qtbot):
    root, child, grandchild = build_tree()
    qtbot.addWidget(root)

    ordered = post_order_including_root(root)

    assert len(ordered) == len(set(id(w) for w in ordered))
    assert set(id(w) for w in ordered) == {id(root), id(child), id(grandchild)}


def test_post_order_on_a_childless_widget_is_just_the_widget(qtbot):
    leaf = QLabel("alone")
    qtbot.addWidget(leaf)

    assert post_order_including_root(leaf) == [leaf]


# ---------------------------------------------------------------------------
# disconnect_all_signals_quietly
# ---------------------------------------------------------------------------

def test_disconnect_severs_the_connection(qtbot):
    emitter = Emitter()
    qtbot.addWidget(emitter)
    received: list[bool] = []
    emitter.fired.connect(lambda: received.append(True))

    disconnect_all_signals_quietly(emitter)
    emitter.fired.emit()

    assert received == []


def test_disconnect_on_an_unconnected_node_never_logs_a_traceback(qtbot):
    """The predecessor called logger.exception for this expected condition --
    on a 900-chip audit, 900 tracebacks per teardown, written to two log files.

    Asserted as "nothing above DEBUG, and no traceback" rather than "a DEBUG
    record was emitted": whether the binding raises on a wildcard disconnect
    with no connections is not a guarantee PyQt makes, so requiring the record
    would be testing the binding rather than the contract.

    Captured with a handler attached directly to the module logger rather than
    via caplog -- bootstrap() strips root handlers, so caplog's capture is not
    reliable once any test in the session has bootstrapped an app.
    """
    node = QLabel("never connected")
    qtbot.addWidget(node)

    captured: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    module_logger = logging.getLogger("cockpit.ui.widgets.qt_lifecycle")
    handler = Capture(level=logging.DEBUG)
    previous_level = module_logger.level
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.DEBUG)
    try:
        disconnect_all_signals_quietly(node)  # must not raise
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(previous_level)

    assert all(r.levelno <= logging.DEBUG for r in captured), (
        "an expected condition must not be reported above DEBUG"
    )
    assert all(r.exc_info is None for r in captured), (
        "no traceback for a node that simply had nothing connected"
    )


def test_disconnect_still_reports_at_debug_when_the_binding_raises(qtbot):
    """The DEBUG path itself, exercised deterministically."""

    class Raises(QObject):
        def disconnect(self, *args, **kwargs):
            raise TypeError("nothing connected")

    captured: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    module_logger = logging.getLogger("cockpit.ui.widgets.qt_lifecycle")
    handler = Capture(level=logging.DEBUG)
    previous_level = module_logger.level
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.DEBUG)
    try:
        disconnect_all_signals_quietly(Raises())
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(previous_level)

    assert len(captured) == 1
    assert captured[0].levelno == logging.DEBUG
    assert captured[0].exc_info is None


# ---------------------------------------------------------------------------
# purge_widget_subtree
# ---------------------------------------------------------------------------

def test_purge_disconnects_the_root_not_only_its_descendants(qtbot):
    """This is F5's sharp edge: root's signals are the ones binding it to the
    long-lived parent view."""
    root, child, _ = build_tree()
    qtbot.addWidget(root)
    from_root: list[bool] = []
    from_child: list[bool] = []
    root.fired.connect(lambda: from_root.append(True))
    child.fired.connect(lambda: from_child.append(True))

    purge_widget_subtree(root)

    root.fired.emit()
    child.fired.emit()
    assert from_root == []
    assert from_child == []


def test_purge_hides_and_detaches_the_root(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    root, _, _ = build_tree(host)
    layout.addWidget(root)

    purge_widget_subtree(root)

    assert root.isHidden()
    assert root.parent() is None


def test_purge_is_idempotent_on_an_already_destroyed_widget(qtbot):
    root, _, _ = build_tree()
    qtbot.addWidget(root)
    purge_widget_subtree(root)
    sip.delete(root)

    purge_widget_subtree(root)  # must not raise


def test_purge_of_a_deep_subtree_severs_every_level(qtbot):
    root = Emitter()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    node = root
    leaves: list[Emitter] = []
    for _ in range(5):
        node = Emitter(node)
        leaves.append(node)
    received: list[int] = []
    for depth, leaf in enumerate(leaves):
        leaf.fired.connect(lambda d=depth: received.append(d))

    purge_widget_subtree(root)

    for leaf in leaves:
        leaf.fired.emit()
    assert received == []


# ---------------------------------------------------------------------------
# The deferred-versus-synchronous decision
# ---------------------------------------------------------------------------

def test_schedule_destruction_is_deferred_not_synchronous(qtbot):
    """Optimize02 R05 specified sip.delete(); the tree ships deleteLater(), and
    section 6 keeps it on step-3 evidence. If this test starts failing, someone
    switched primitives -- which is a decision, not a refactor."""
    widget = QLabel("doomed")
    qtbot.addWidget(widget)

    schedule_destruction(widget)

    assert not is_destroyed(widget), "destruction must be queued, not immediate"


def test_is_destroyed_reports_the_cpp_half(qtbot):
    widget = QLabel("doomed")
    qtbot.addWidget(widget)
    assert not is_destroyed(widget)

    sip.delete(widget)

    assert is_destroyed(widget)
