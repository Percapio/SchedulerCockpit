"""Lifecycle management utilities for PyQt6 widgets."""

from PyQt6.QtWidgets import QWidget, QLayout
from PyQt6.QtCore import QObject, Qt
from PyQt6 import sip

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
        pass

    print(f"    [purge] sip.delete on {type(target)}")
    sip.delete(target)


def purge_widget_subtree(root: QWidget) -> None:
    """
    Safely purges a widget and its descendants.
    """
    if sip.isdeleted(root):
        return

    print(f"[purge] finding descendants of {type(root)}")
    from PyQt6.QtWidgets import QWidget, QLayout
    
    # Intentionally avoid findChildren(QObject) to prevent creating wrappers for internal C++ objects
    descendants = []
    descendants.extend(root.findChildren(QWidget, options=Qt.FindChildOption.FindChildrenRecursively))
    descendants.extend(root.findChildren(QLayout, options=Qt.FindChildOption.FindChildrenRecursively))
    
    # We must also include our custom QObjects that are not QWidgets/QLayouts
    # MPNLabelFilter inherits from QObject
    from cockpit.ui.widgets.audit_bom_panel import MPNLabelFilter
    descendants.extend(root.findChildren(MPNLabelFilter, options=Qt.FindChildOption.FindChildrenRecursively))

    for descendant in reversed(descendants):
        _disconnect_and_delete(descendant)

    _disconnect_and_delete(root)


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
                except: pass
            w.setParent(None)
            widgets.append(w)
    return widgets
