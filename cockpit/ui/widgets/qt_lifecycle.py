"""Lifecycle management utilities for PyQt6 widgets."""

from PyQt6.QtWidgets import QWidget, QLayout
from PyQt6.QtCore import QObject, Qt
from PyQt6 import sip
import logging
logger = logging.getLogger(__name__)


def _disconnect_and_delete(target: QObject) -> None:
    """
    Sever every signal on `target`, then sip.delete its C++ object.
    """
    if sip.isdeleted(target):
        return
    print(f"    [purge] disconnecting {type(target)}")
    try:
        target.disconnect()
    except TypeError:
        logger.exception('Exception caught in qt_lifecycle')
        pass

    print(f"    [purge] deleteLater on {type(target)}")
    target.deleteLater()


def purge_widget_subtree(root: QWidget) -> None:
    """
    Safely purges a widget. Relies on Qt's internal destructor to clean up children.
    """
    if sip.isdeleted(root):
        return
    root.hide()
    root.deleteLater()


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
