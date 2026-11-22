"""One document-update run on the UI thread (Phase 55b §5).

DocumentUpdateRun owns the update worker and its thread, feeds it snapshots in batches between
event-loop turns, watches its share calls, applies each plan as it arrives, defers every unlink until
the worker has stopped preparing, and shows the progress, preview and results dialogs for a PREVIEW
run. A run of placements alone (the check after ingest) shows no dialog; MainWindow words its toast.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from PyQt6 import sip
from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from cockpit.services import document_updates as du
from cockpit.services import update_run as ur
from cockpit.thread_retention import ThreadLabel, thread_retention
from cockpit.ui.widgets.qt_lifecycle import FETCH_WATCHDOG_TIMEOUT_MS
from cockpit.ui.workers.update_worker import UpdateWorker

logger = logging.getLogger(__name__)

PROGRESS_DELAY_MS = 500


@dataclass
class RunServices:
    repositories: du.FamilyUpdateRepositories
    runtime_calc: Any
    parsers: Any
    source_root: pathlib.Path
    storage_root: pathlib.Path
    vocabulary: Any = None
    requirements_refresh: Optional[Callable[[Sequence[int]], None]] = None
    orphan: Optional[Callable[[QThread, QObject], None]] = None


@dataclass
class RunResult:
    request: ur.RunRequest
    summary: ur.UpdateRunSummary
    stop: Optional[ur.StopReason]
    records: dict
    applied_audits: frozenset[int] = frozenset()
    abandoned: bool = False


def stop_message(
    reason: Optional[ur.StopReason],
    not_done: int,
    preparing: bool,
    bound_ms: int = FETCH_WATCHDOG_TIMEOUT_MS,
) -> Optional[str]:
    """§5.5's words for a stop, or None when the run ended normally or you cancelled it."""
    if isinstance(reason, ur.NoAnswer):
        what = "not updated" if preparing else "not checked"
        noun = "family" if not_done == 1 else "families"
        return f"No answer from the share within {bound_ms / 1000:g} s; {not_done:,} {noun} {what}"
    if isinstance(reason, du.ShareUnreachable):
        return f"The share can't be reached ({reason.cause})"
    if isinstance(reason, ur.WorkerFailed):
        return f"The check stopped: {reason.cause}"
    return None


class DocumentUpdateRun(QObject):
    finished = pyqtSignal(object)  # RunResult

    def __init__(
        self,
        request: ur.RunRequest,
        keys: Sequence[du.FamilyKey],
        services: RunServices,
        dialog_parent: Optional[QWidget] = None,
        watchdog_ms: int = FETCH_WATCHDOG_TIMEOUT_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.request = request
        self.keys = list(keys)
        self.services = services
        self.dialog_parent = dialog_parent
        self.records: dict[du.FamilyKey, ur.FamilyRecord] = {}
        self.cleanup = du.DeferredCleanup()
        self.applied_audits: set[int] = set()
        self._next = 0
        self._dropped = 0
        self._probed = 0
        self._to_compare = 0
        self._compared = 0
        self._prepare_keys: tuple = ()
        self._ended = False
        self._applied = False
        self._comparing_done = False
        self._progress: Optional[QProgressDialog] = None
        self._setting_progress = False
        self.dialog = None
        self.thread: Optional[QThread] = None
        self.worker: Optional[UpdateWorker] = None

        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.timeout.connect(self._take_batch)
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(watchdog_ms)
        self._watchdog.timeout.connect(self._on_watchdog)

    @property
    def preview_mode(self) -> bool:
        return self.request.confirm == ur.ConfirmMode.PREVIEW

    @property
    def active(self) -> bool:
        return not self._ended

    def _dialog_host(self) -> Optional[QWidget]:
        """What the run's dialogs show over: dialog_parent while it lives, else the window.

        Settings deletes itself on close, and you can close it in the half second before the progress
        dialog appears.
        """
        host = self.dialog_parent
        if host is not None and not sip.isdeleted(host):
            return host
        window = self.parent()
        return window if isinstance(window, QWidget) else None

    def _live_progress(self) -> Optional[QProgressDialog]:
        """The progress dialog, or None once it is closed or was deleted with its parent."""
        if self._progress is not None and sip.isdeleted(self._progress):
            self._progress = None
        return self._progress

    # --- starting ---

    def start(self) -> None:
        # Patch 14 §10.7: M1 counts update runs from these lines.
        logger.info(
            "Update run started: %s, %d families, run %s",
            self.request.confirm.name, len(self.keys), getattr(self.request, "run_id", "?"),
        )
        self.thread = QThread()
        self.worker = UpdateWorker(self.request, self.services.source_root, self.services.storage_root,
                                   self.services.parsers)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.run_event.connect(self._on_event)
        self.worker.beacon.started.connect(self._watchdog.start)
        self.worker.beacon.finished.connect(self._watchdog.stop)
        if self.preview_mode:
            self._progress = QProgressDialog("Checking families…", "Cancel", 0, max(len(self.keys), 1),
                                             self._dialog_host())
            self._progress.setWindowTitle("Check for document updates")
            self._progress.setWindowModality(Qt.WindowModality.ApplicationModal)
            self._progress.setMinimumDuration(PROGRESS_DELAY_MS)
            self._progress.setAutoClose(False)
            self._progress.setAutoReset(False)
            self._progress.canceled.connect(self.cancel)
            self._progress.setValue(0)
        self.thread.start()
        self._batch_timer.start(0)

    def _take_batch(self) -> None:
        """§5.1: SNAPSHOT_BATCH snapshots per event-loop turn, queued to the worker as read."""
        if self._ended or self.worker is None:
            return
        batch = []
        try:
            while len(batch) < ur.SNAPSHOT_BATCH and self._next < len(self.keys):
                key = self.keys[self._next]
                self._next += 1
                snapshot = du.snapshot_family(key, self.services.repositories, vocabulary=self.services.vocabulary)
                if snapshot is None:
                    self._dropped += 1
                    continue
                self.records[key] = ur.FamilyRecord(key)
                batch.append(snapshot)
        except Exception as error:
            logger.error("Snapshots for the update run failed: %s", type(error).__name__)
            self._abandon_or_end(ur.WorkerFailed(type(error).__name__))
            return
        if batch:
            self.worker.commands.put(ur.Snapshots(tuple(batch)))
        if self._next < len(self.keys):
            self._batch_timer.start(0)
        else:
            self.worker.commands.put(ur.SnapshotsComplete())
        self._show_progress()

    def _total(self) -> int:
        return max(len(self.keys) - self._dropped, 0)

    def _show_progress(self) -> None:
        """A modal QProgressDialog's setValue runs processEvents, so the run's own events, and a click
        on Cancel, can close the dialog inside it; it is deleted only once setValue has returned."""
        progress = self._live_progress()
        if progress is None or self._setting_progress:
            return
        progress.setMaximum(max(self._total(), 1))
        progress.setLabelText(f"Checked {self._probed:,} of {self._total():,} families")
        self._setting_progress = True
        try:
            progress.setValue(min(self._probed, self._total()))
        finally:
            self._setting_progress = False
        if self._progress is None and not sip.isdeleted(progress):
            progress.deleteLater()

    # --- the worker's events ---

    def _on_event(self, event: Any) -> None:
        if self._ended:
            return
        if isinstance(event, ur.Probed):
            record = self.records.setdefault(event.digest.key, ur.FamilyRecord(event.digest.key))
            record.digest = event.digest
            self._probed += 1
            self._to_compare += 1 if event.digest.to_compare else 0
            self._show_progress()
        elif isinstance(event, ur.ProbingDone):
            self._close_progress()
            if self.preview_mode:
                self._open_dialog()
        elif isinstance(event, ur.PreviewReady):
            key = event.preview.probe.snapshot.key
            first = self.records[key].preview is None
            if self.dialog is not None:
                self.dialog.update_preview(key, event.preview)
            else:
                self.records[key].preview = event.preview
            if first:
                self._compared += 1
            self._comparing_text()
        elif isinstance(event, ur.ComparingDone):
            self._comparing_done = True
            if self.dialog is not None:
                self.dialog.comparing_done()
                if self._to_compare == 0:
                    self.dialog.set_progress("Nothing to update")
                elif event.stopped_early:
                    self.dialog.set_progress(f"Compared {self._compared:,} of {self._to_compare:,}; stopped")
                else:
                    self.dialog.set_progress("Ready")
        elif isinstance(event, ur.PlanReady):
            self._apply(event.plan)
        elif isinstance(event, ur.PrepareFailedFor):
            self.cleanup.discard(event.failed.copied_by_this_run)
            self._set_outcome(event.key, ur.NotApplied(event.failed.failure))
        elif isinstance(event, ur.RunStopped):
            self._end(event.reason)
        elif isinstance(event, ur.RunDone):
            self._end(None)

    def _comparing_text(self) -> None:
        if self.dialog is not None and not self._comparing_done:
            self.dialog.set_progress(f"Comparing {self._compared:,} of {self._to_compare:,}…")

    def _apply(self, plan: du.FamilyUpdatePlan) -> None:
        outcome = du.apply_family_update(
            plan, self.services.repositories, self.services.runtime_calc,
            self.services.requirements_refresh, cleanup=self.cleanup,
        )
        if isinstance(outcome, du.FamilyUpdated):
            self.applied_audits.update(outcome.family)
            self._set_outcome(plan.key, ur.Applied(outcome))
        else:
            self._set_outcome(plan.key, ur.NotApplied(outcome))
        if self.dialog is not None and self._applied:
            done = sum(1 for k in self._prepare_keys if self.records[k].outcome is not None)
            self.dialog.set_progress(f"Applying {done:,} of {len(self._prepare_keys):,}…")

    def _set_outcome(self, key: du.FamilyKey, outcome: ur.FamilyOutcome) -> None:
        record = self.records.setdefault(key, ur.FamilyRecord(key))
        record.outcome = outcome
        if self.dialog is not None:
            self.dialog.set_outcome(key)

    # --- the dialogs ---

    def _close_progress(self) -> None:
        progress = self._live_progress()
        if progress is None:
            return
        self._progress = None
        try:
            progress.canceled.disconnect(self.cancel)
        except (TypeError, RuntimeError):
            pass
        progress.close()
        if not self._setting_progress:
            progress.deleteLater()

    def _open_dialog(self) -> None:
        from cockpit.ui.widgets.document_update_dialog import DocumentUpdateDialog

        self.dialog = DocumentUpdateDialog(self.records, self._dialog_host())
        self.dialog.finished.connect(self.dialog.deleteLater)
        self.dialog.apply_requested.connect(self._on_apply_requested)
        self.dialog.pick_requested.connect(self._on_pick_requested)
        self.dialog.stop_comparing_requested.connect(self._on_stop_comparing)
        self.dialog.cancel_requested.connect(self.cancel)
        self.dialog.show_rows([k for k in self.keys if k in self.records])
        self._comparing_text()
        self.dialog.show()

    def _on_apply_requested(self, keys: tuple) -> None:
        if self._ended or self.worker is None:
            return
        self._applied = True
        self._prepare_keys = tuple(keys)
        self.dialog.set_progress(f"Applying 0 of {len(keys):,}…")
        self.worker.commands.put(ur.Prepare(tuple(keys)))

    def _on_pick_requested(self, key: du.FamilyKey, choices: dict) -> None:
        if self._ended or self.worker is None:
            return
        self.worker.commands.put(ur.Pick(key, dict(choices)))

    def _on_stop_comparing(self) -> None:
        if self.worker is not None and not self._ended:
            self.worker.commands.put(ur.StopComparing())

    # --- stopping ---

    def cancel(self) -> None:
        """§5.5 and §5.2: Finish after comparing ends without Apply; stop otherwise."""
        if self._ended or self.worker is None:
            return
        self._batch_timer.stop()
        if self._applied:
            self.worker.stop.set()
            if self.dialog is not None:
                self.dialog.set_progress("Stopping at the next safe point…")
            return
        if self._comparing_done:
            self.worker.commands.put(ur.Finish())
        else:
            self.worker.stop.set()
        self._close_progress()
        if self.dialog is not None:
            self.dialog.finish_and_close()
            self.dialog = None

    def _on_watchdog(self) -> None:
        if not self._ended:
            self._abandon_or_end(ur.NoAnswer())

    def _abandon_or_end(self, reason: ur.StopReason) -> None:
        """Phase 55 §7's abandonment: stop, disconnect, park the thread; then end the run here."""
        self._batch_timer.stop()
        self._watchdog.stop()
        if self.worker is not None and self.thread is not None:
            self.worker.stop.set()
            for signal, slot in (
                (self.worker.run_event, self._on_event),
                (self.worker.beacon.started, self._watchdog.start),
                (self.worker.beacon.finished, self._watchdog.stop),
            ):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            thread, worker = self.thread, self.worker
            self.thread = None
            self.worker = None
            try:
                thread.quit()
            except RuntimeError:
                pass
            if self.services.orphan is not None:
                self.services.orphan(thread, worker)
        self._end(reason, abandoned=True)

    def _end(self, reason: Optional[ur.StopReason], abandoned: bool = False) -> None:
        if self._ended:
            return
        self._ended = True
        self._batch_timer.stop()
        self._watchdog.stop()
        if not abandoned and self.thread is not None:
            self.thread.quit()
            if self.thread.wait(5000):
                self.worker.deleteLater()
                self.thread.deleteLater()
            else:
                # Patch 14 §5.2: deleting a QThread that still runs aborts the process, so a thread that
                # won't finish is parked like an abandoned one, and the run reports itself abandoned.
                logger.warning("The update worker didn't finish within 5 s of the run's end; it is parked")
                thread, worker = self.thread, self.worker
                if self.services.orphan is not None:
                    self.services.orphan(thread, worker)
                else:
                    thread.finished.connect(worker.deleteLater)
                    thread.finished.connect(thread.deleteLater)
                    thread_retention().retain(thread, worker, ThreadLabel.UPDATE_CHECK)
                abandoned = True
            self.thread = None
            self.worker = None
        try:
            self.cleanup.flush(self.services.repositories.source_files)
        except Exception as error:
            # By kind only: an OSError's message names the stored file.
            logger.warning("Unlinking the run's files failed (%s); the startup sweep will", type(error).__name__)
        for key in self._prepare_keys:
            record = self.records.get(key)
            if record is not None and record.outcome is None:
                record.outcome = ur.NotReached()
        summary = ur.summarize(len(self.keys), self._dropped, list(self.records.values()), reason)
        not_done = (sum(1 for k in self._prepare_keys if isinstance(self.records[k].outcome, ur.NotReached))
                    if self._applied else summary.not_checked)
        message = stop_message(reason, not_done, self._applied, self._watchdog.interval())
        self._close_progress()
        if self.dialog is not None:
            text = ur.summary_line(summary)
            if message:
                text = f"{message}. {text}"
            self.dialog.run_finished(text)
        elif message and self.preview_mode:
            QMessageBox.warning(self._dialog_host(), "Check for document updates", message)
        logger.info(
            "Update run ended: %s, abandoned=%s, %d audits applied",
            type(reason).__name__ if reason is not None else "done", abandoned, len(self.applied_audits),
        )
        self.finished.emit(RunResult(
            request=self.request, summary=summary, stop=reason, records=self.records,
            applied_audits=frozenset(self.applied_audits), abandoned=abandoned,
        ))

    def shutdown_for_close(self) -> None:
        """closeEvent (§3.3): the run is stopped and abandoned, its dialogs closed, its unlinks run."""
        if self._ended:
            return
        if self.dialog is not None:
            self.dialog.finish_and_close()
            self.dialog = None
        self._close_progress()
        if self.thread is not None and self.thread.isRunning():
            self._abandon_or_end(ur.CancelledByYou())
        else:
            self._end(ur.CancelledByYou())


def new_run_id() -> str:
    return uuid.uuid4().hex
