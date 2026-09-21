"""Lifecycle management utilities for PyQt6 widgets."""

import weakref
from typing import Callable

from PyQt6.QtWidgets import QWidget, QLayout
from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6 import sip
import logging
logger = logging.getLogger(__name__)

_DIRECT_CHILDREN_ONLY = Qt.FindChildOption.FindDirectChildrenOnly

# Ceiling on any one scan of the source-root share: the job/drawing fetch and
# the source-root probe enumerate the same directories over the same SMB mounts,
# so they wait on the same clock. Defined here rather than in main_window
# because settings_dialog needs it too, and main_window already imports
# settings_dialog -- a widget cannot import the window that owns it.
FETCH_WATCHDOG_TIMEOUT_MS = 20_000

# Probes outlive the widget that started them. A widget closed mid-probe severs
# its handler and returns without waiting, so the thread reference has to live
# somewhere that is not a widget: Python collecting a running QThread aborts the
# process. Entries are discarded on finished.
_IN_FLIGHT_PROBES: set = set()

# Admissions, so a second launch for one owner can supersede the first. Separate
# from the thread registry because superseding severs a handler and must never
# drop a thread.
_ADMISSIONS: set = set()


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


# --- Patch 11: the bounded probe seam ---------------------------------------

class _BoundedProbeWorker(QObject):
    """Runs one caller-supplied callable off the UI thread.

    Holds the callable as a value handed over before the thread started, so the
    owner can empty its widgets at any moment without disturbing work in
    flight.
    """

    succeeded = pyqtSignal(object)
    raised = pyqtSignal(object)

    def __init__(self, work: Callable[[], object]) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        try:
            outcome = self._work()
        except BaseException as exc:  # noqa: BLE001 -- see below
            # An unhandled exception in a PyQt slot terminates the process, and
            # `started` invokes this as a slot. Catching everything here is what
            # turns a programming error into a status line.
            logger.exception("bounded probe work raised")
            self.raised.emit(exc)
            return
        self.succeeded.emit(outcome)


class ProbeAdmission:
    """One launched probe, from the owner's side.

    post: is_in_flight() is true from launch until a terminal handler runs, the
          watchdog fires, abandon() is called, or a later launch for the same
          owner supersedes it -- whichever happens first.
    """

    def __init__(self, owner: QObject, thread: QThread, worker: _BoundedProbeWorker,
                 watchdog: QTimer) -> None:
        # Weak, so an admission cannot keep a closed dialog alive.
        self._owner_ref = weakref.ref(owner)
        self.thread = thread
        self.worker = worker
        self._watchdog = watchdog
        self._settled = False
        self._handlers: list = []

    def owner(self) -> QObject | None:
        return self._owner_ref()

    def is_in_flight(self) -> bool:
        return not self._settled

    def _settle(self) -> None:
        self._settled = True
        try:
            self._watchdog.stop()
        except RuntimeError:
            # The watchdog is parented to the owner; a destroyed owner takes it
            # with it, and a stopped-by-destruction timer is stopped enough.
            pass

    def _sever(self) -> None:
        """Disconnects the owner's handlers and no others.

        The worker's terminal signals are also wired to thread.quit(), and that
        wiring is never severed. Disconnecting a signal wholesale here would
        leave the thread with nothing to end it -- the leak that
        _abandon_fetch_worker demonstrates.
        """
        for signal, handler in self._handlers:
            try:
                signal.disconnect(handler)
            except (TypeError, RuntimeError):
                pass
        self._handlers = []

    def _ask_thread_to_exit(self) -> None:
        try:
            # quit() is not wait(). It posts a request and returns. A thread
            # still inside its blocking call has not entered exec() yet, so this
            # latches and exec() returns immediately when it is reached.
            self.thread.quit()
        except RuntimeError:
            pass

    def abandon(self) -> None:
        """Severs this admission from its owner without waiting on its thread.

        post: no terminal handler will run. The watchdog is stopped, the thread
              is asked to exit and does so once its worker's blocking call
              returns. Abandoning a settled admission is a no-op.
        raises: nothing
        """
        if self._settled:
            return
        self._settle()
        self._sever()
        self._ask_thread_to_exit()

    def _abandon_from_owner_destruction(self) -> None:
        """The same intent as abandon(), minus every call that is unsafe here.

        This runs inside the owner's destroyed signal, which Qt emits partway
        through ~QObject. Disconnecting a signal at that point walks connection
        lists Qt is already tearing down and faults; stopping the watchdog
        touches a QTimer that died with its parent a moment ago.

        Neither is needed. Settling alone is sufficient to stop delivery,
        because every terminal handler checks is_in_flight() before calling the
        owner back. The disconnect in abandon() is an economy, not the
        guarantee.
        """
        if self._settled:
            return
        self._settled = True
        self._handlers = []
        self._ask_thread_to_exit()


def _supersede_previous(owner: QObject) -> None:
    for admission in list(_ADMISSIONS):
        if admission.is_in_flight() and admission.owner() is owner:
            admission.abandon()


def start_bounded_probe(
    owner: QObject,
    work: Callable[[], object],
    watchdog_ms: int,
    on_outcome: Callable[[object], None],
    on_timeout: Callable[[], None],
    on_error: Callable[[BaseException], None],
) -> ProbeAdmission:
    """Runs one off-thread unit of work that cannot reach a destroyed widget.

    pre:  owner is alive and lives on the UI thread; work is callable on a
          worker thread and performs no Qt widget access; watchdog_ms > 0
    post: exactly one of on_outcome, on_timeout or on_error runs, on the UI
          thread, unless the admission was abandoned or superseded first, in
          which case none runs. Launching supersedes any unsettled admission
          previously issued to the same owner. The thread is retained by a
          module-level registry from launch until it emits finished, never by
          an attribute on owner, so a second launch cannot drop the first.
    raises: nothing
    """
    _supersede_previous(owner)

    thread = QThread()
    worker = _BoundedProbeWorker(work)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    entry = (thread, worker)
    _IN_FLIGHT_PROBES.add(entry)

    watchdog = QTimer(owner)
    watchdog.setSingleShot(True)
    admission = ProbeAdmission(owner, thread, worker, watchdog)
    _ADMISSIONS.add(admission)

    # Connected first and never severed: this is what ends the thread on every
    # path the worker reaches, including the ones where the owner has stopped
    # listening.
    worker.succeeded.connect(thread.quit)
    worker.raised.connect(thread.quit)
    worker.succeeded.connect(worker.deleteLater)
    worker.raised.connect(worker.deleteLater)
    thread.finished.connect(lambda: _IN_FLIGHT_PROBES.discard(entry))
    thread.finished.connect(lambda: _ADMISSIONS.discard(admission))
    thread.finished.connect(thread.deleteLater)

    def deliver_outcome(outcome: object) -> None:
        if not admission.is_in_flight():
            return
        admission._settle()
        on_outcome(outcome)

    def deliver_error(exc: BaseException) -> None:
        if not admission.is_in_flight():
            return
        admission._settle()
        on_error(exc)

    def deliver_timeout() -> None:
        if not admission.is_in_flight():
            return
        admission._settle()
        # Abandonment, not cancellation: a blocking read cannot be interrupted
        # from the UI thread, and wait() here would reproduce the freeze this
        # timer exists to escape.
        admission._sever()
        try:
            thread.quit()
        except RuntimeError:
            pass
        on_timeout()

    worker.succeeded.connect(deliver_outcome)
    worker.raised.connect(deliver_error)
    admission._handlers = [
        (worker.succeeded, deliver_outcome),
        (worker.raised, deliver_error),
    ]
    watchdog.timeout.connect(deliver_timeout)

    # The caller is expected to abandon on close; this makes forgetting
    # harmless rather than fatal. Deliberately not abandon(): see
    # _abandon_from_owner_destruction.
    owner.destroyed.connect(lambda *_: admission._abandon_from_owner_destruction())

    watchdog.start(watchdog_ms)
    thread.start()
    return admission
