"""Lifecycle management utilities for PyQt6 widgets."""

from PyQt6.QtWidgets import QWidget, QLayout
from PyQt6.QtCore import QObject, Qt
from PyQt6 import sip
import logging
logger = logging.getLogger(__name__)

_DIRECT_CHILDREN_ONLY = Qt.FindChildOption.FindDirectChildrenOnly


def is_destroyed(target: QObject) -> bool:
    """True when the C++ object behind `target` has already been destroyed."""
    return sip.isdeleted(target)


def schedule_destruction(target: QObject) -> None:
    """Queue `target` for destruction at the next event-loop turn of the level
    at which this was called.

    This is deleteLater(), not sip.delete(). Optimize02 R05 specified synchronous
    severance; the tree shipped deferred deletion. Optimize06 section 6 routed
    that choice to a decision on step-3 evidence, and the evidence says keep it:
    the churn baseline shows every row-class wrapper count returning to zero on
    refcount alone at the cycle it is dropped, so deferred deletion is not
    retaining anything. It is also the safer primitive -- sip.delete() on a
    widget still referenced by a Qt event already in the queue is a hard crash,
    where deferred deletion would at worst be a leak.

    The cost that remains, and is accepted: DeferredDelete events posted inside a
    nested QDialog.exec() loop are held until that loop unwinds, so teardown
    timing under a modal dialog is later than Optimize02 specified.
    """
    target.deleteLater()


def post_order_including_root(root: QWidget) -> list[QWidget]:
    """
    Intent:  Depth-first post-order traversal of the widget subtree, root last.
    Pre:     root is not destroyed.
    Post:    Every descendant appears before its parent; root is the final
             element. Direct-children-only traversal at each level, so a widget
             is visited exactly once.

    The predecessor walked root.findChildren(...) and never appended root, so
    the disconnect loop covered every node except the one whose signals bind it
    to the long-lived parent view -- which is the only node that matters.
    """
    ordered: list[QWidget] = []
    for child in root.findChildren(QWidget, options=_DIRECT_CHILDREN_ONLY):
        if not is_destroyed(child):
            ordered.extend(post_order_including_root(child))
    ordered.append(root)
    return ordered


def disconnect_all_signals_quietly(node: QObject) -> None:
    """
    Intent:  Sever every outbound connection on `node`.
    Post:    node has no connected signals. The "node had nothing connected"
             case is logged at DEBUG, not as an exception.

    The predecessor called logger.exception here, producing a full traceback for
    an expected condition on every leaf widget of every purge -- on a 900-chip
    audit, 900 tracebacks per teardown.
    """
    try:
        node.disconnect()
    except (TypeError, RuntimeError):
        logger.debug("no connections to sever on %s", type(node).__name__)


def purge_widget_subtree(root: QWidget) -> None:
    """
    Intent:  Sever every signal connection in the subtree rooted at root,
             including root's own, then destroy the widget.
    Pre:     root is not already destroyed.
    Post:    No signal on root or any descendant remains connected. root is
             hidden, detached from its parent, and queued for destruction.
    Raises:  Nothing. A node that cannot be disconnected is logged at DEBUG and
             skipped; traversal continues.
    """
    if is_destroyed(root):
        return

    for node in post_order_including_root(root):
        disconnect_all_signals_quietly(node)

    root.hide()
    root.setParent(None)
    schedule_destruction(root)


def _drain_layout_widgets(layout: QLayout) -> list[QWidget]:
    """
    Remove and return every widget currently held by `layout`, in
    the order they were added. Non-widget items (spacers, stretches)
    are removed and discarded.
    """
    widgets: list[QWidget] = []
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            if hasattr(w, "cleanup"):
                try: w.cleanup()
                except Exception:
                    logger.exception('Exception caught in qt_lifecycle')
            widgets.append(w)
    return widgets
