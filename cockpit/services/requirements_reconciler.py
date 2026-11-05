"""Background reconciler and completion warning for Phase 54 process requirements (§7.2, §7.3, §11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from cockpit.persistence.clock import utcnow
from cockpit.persistence.repositories.requirements import AuditRequirementsRepository
from cockpit.services.requirements import (
    DEFAULT_VOCABULARY,
    RequirementKind,
    RequirementScan,
    RequirementState,
    RequirementSummary,
    RequirementVocabulary,
    ScanInputs,
    XrayCellState,
    XrayTick,
    scan_documents,
    scan_fingerprint,
    xray_cell_state,
)

logger = logging.getLogger(__name__)

RENDER_SHUTDOWN_TIMEOUT_MS: int = 10_000


@dataclass(frozen=True)
class ScanJob:
    audit_id: int
    inputs: ScanInputs
    prior_states: dict[RequirementKind, RequirementState] | None


@dataclass(frozen=True)
class PassSummary:
    """What one reconciler pass did (§7.2).

    jobs counts what stale_scan_jobs listed; written counts the scans
    write_scan_if_current stored; dropped_as_stale counts those it refused, and
    those dropped because a worker-thread transaction held the connection;
    changed_state counts written scans whose states differ from the row they
    replaced, or that filled a missing row; failed counts jobs that couldn't be
    completed; stopped_early is true when a stop request or a worker failure
    ended the pass before its last job.
    """

    jobs: int
    written: int
    dropped_as_stale: int
    changed_state: int
    failed: int
    stopped_early: bool


def _extract_wash_text(raw_meta: str | dict | None) -> str | None:
    if raw_meta is None:
        return None
    if isinstance(raw_meta, str):
        try:
            meta = json.loads(raw_meta)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw_meta, dict):
        meta = raw_meta
    else:
        return None
    val = meta.get("process_clean")
    return str(val) if val is not None else None


def _build_scan_inputs_for_audit(
    conn: sqlite3.Connection,
    audit_id: int,
    raw_traveler_metadata: str | dict | None,
    vocabulary: RequirementVocabulary,
) -> ScanInputs:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT file_category, local_storage_path, file_hash
        FROM source_files
        WHERE audit_id = ? AND file_category IN ('Notes', 'BOM')
        """,
        (audit_id,),
    )
    notes_path = Path("")
    notes_hash = ""
    bom_path = Path("")
    bom_hash = ""
    for sf in cur.fetchall():
        cat = sf["file_category"]
        p = sf["local_storage_path"]
        p_obj = p if isinstance(p, Path) else Path(str(p))
        h = str(sf["file_hash"] or "")
        if cat == "Notes":
            notes_path = p_obj
            notes_hash = h
        elif cat == "BOM":
            bom_path = p_obj
            bom_hash = h

    wash_text = _extract_wash_text(raw_traveler_metadata)
    return ScanInputs(
        notes_path=notes_path,
        notes_hash=notes_hash,
        bom_path=bom_path,
        bom_hash=bom_hash,
        traveler_wash_text=wash_text,
        vocabulary=vocabulary,
    )


def stale_scan_jobs(
    conn: sqlite3.Connection,
    vocabulary: RequirementVocabulary,
) -> list[ScanJob]:
    """Lists open audits whose stored requirement scan is missing or stale (§7.2).

    Ordered oldest scan first (missing scans first, then scanned_at ASC, id ASC).
    """
    req_repo = AuditRequirementsRepository(conn)
    summaries = req_repo.find_summaries_for_open_audits()

    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.id AS audit_id,
               a.traveler_metadata AS traveler_metadata,
               r.scanned_at AS scanned_at
        FROM active_audits a
        LEFT JOIN audit_requirements r ON r.audit_id = a.id
        ORDER BY (r.scanned_at IS NOT NULL) ASC, r.scanned_at ASC, a.id ASC
        """
    )
    rows = cur.fetchall()
    jobs: list[ScanJob] = []
    for row in rows:
        audit_id = int(row["audit_id"])
        inputs = _build_scan_inputs_for_audit(
            conn, audit_id, row["traveler_metadata"], vocabulary
        )
        expected_fp = scan_fingerprint(inputs)
        summary = summaries.get(audit_id)
        if summary is not None and summary.fingerprint == expected_fp:
            continue
        prior_states = dict(summary.states) if summary is not None else None
        jobs.append(
            ScanJob(
                audit_id=audit_id,
                inputs=inputs,
                prior_states=prior_states,
            )
        )
    return jobs


def write_scan_if_current(
    conn: sqlite3.Connection,
    audit_id: int,
    scan: RequirementScan,
    vocabulary: RequirementVocabulary,
) -> bool:
    """Writes a scan only when it still describes the audit's current inputs (§7.2).

    pre:  UI thread, with no worker-thread transaction open on the shared
          connection; the reconciler's can_write gate ensures that, and every
          other writer of an audit's inputs runs on this thread, so nothing
          changes them between the read below and the write
    post: True iff the audit still exists and scan_fingerprint of its current
          inputs equals scan.fingerprint, in which case its row now holds the
          scan; False otherwise, and nothing is written

    No BEGIN IMMEDIATE: an ingest runs its savepoint on a worker thread over
    this same connection, and a BEGIN inside it fails ("cannot start a
    transaction within a transaction"). The upsert is one statement, so it is
    atomic on its own.
    """
    cur = conn.cursor()
    cur.execute("SELECT traveler_metadata FROM active_audits WHERE id = ?", (audit_id,))
    audit_row = cur.fetchone()
    if not audit_row:
        return False

    current_inputs = _build_scan_inputs_for_audit(
        conn, audit_id, audit_row["traveler_metadata"], vocabulary
    )
    if scan_fingerprint(current_inputs) != scan.fingerprint:
        return False

    AuditRequirementsRepository(conn).upsert_scan(audit_id, scan)
    return True


def run_pass(
    conn: sqlite3.Connection,
    jobs: list[ScanJob],
    vocabulary: RequirementVocabulary | None = None,
) -> PassSummary:
    """Runs one reconciler pass synchronously, scanning and writing each job in order (§7.2)."""
    written = dropped = changed = failed = 0
    for job in jobs:
        vocab = vocabulary or job.inputs.vocabulary
        scan = scan_documents(job.inputs)
        try:
            wrote = write_scan_if_current(conn, job.audit_id, scan, vocab)
        except sqlite3.Error as exc:
            logger.warning("Requirement scan for audit %s not written: %s", job.audit_id, type(exc).__name__)
            failed += 1
            continue
        if not wrote:
            dropped += 1
            continue
        written += 1
        if job.prior_states != scan.states:
            changed += 1
    return PassSummary(
        jobs=len(jobs), written=written, dropped_as_stale=dropped,
        changed_state=changed, failed=failed, stopped_early=False,
    )


def pending_xray_warning(
    conn: sqlite3.Connection,
    audit_id: int,
    vocabulary: RequirementVocabulary = DEFAULT_VOCABULARY,
) -> str | None:
    """Returns the extra warning sentence when X-ray & save images is unticked or stale (§11)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, traveler_metadata,
               xray_images_done, xray_images_done_task, xray_images_done_at
        FROM active_audits
        WHERE id = ?
        """,
        (audit_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    raw_done_at = row["xray_images_done_at"]
    if isinstance(raw_done_at, str):
        done_at = datetime.fromisoformat(raw_done_at)
        if done_at.tzinfo is None:
            done_at = done_at.replace(tzinfo=timezone.utc)
    else:
        done_at = raw_done_at

    tick = XrayTick(
        is_done=bool(row["xray_images_done"]),
        task_at_tick=row["xray_images_done_task"],
        ticked_at=done_at,
    )

    req_repo = AuditRequirementsRepository(conn)
    summary = req_repo.find_summary(audit_id)
    if summary is None:
        inputs = _build_scan_inputs_for_audit(
            conn, audit_id, row["traveler_metadata"], vocabulary
        )
        scan = scan_documents(inputs)
        write_scan_if_current(conn, audit_id, scan, vocabulary)
        summary = req_repo.find_summary(audit_id)
        if summary is None:
            summary = RequirementSummary(
                states=scan.states,
                unknown_reasons=scan.unknown_reasons,
                xray_task=scan.xray_task,
                fingerprint=scan.fingerprint,
                scanned_at=utcnow(),
            )

    state = xray_cell_state(summary, tick)
    if state == XrayCellState.PENDING:
        return "Its X-ray & save-images task isn't ticked."
    if state == XrayCellState.DONE_STALE:
        return "Its X-ray & save-images tick predates a change in the X-ray requirements."
    if state == XrayCellState.UNKNOWN:
        reason = (
            summary.unknown_reasons.get(RequirementKind.XRAY_SAVE_IMAGES)
            or summary.unknown_reasons.get(RequirementKind.XRAY)
            or "unknown reason"
        )
        return (
            f"Its X-ray requirements couldn't be read ({reason}), "
            "so an X-ray & save-images task may be outstanding."
        )
    return None


class _ReconcilerWorker(QObject):
    """Worker running on a background QThread. Never touches SQLite or QSettings (§7.2)."""

    job_scanned = pyqtSignal(int, object, object)  # audit_id, RequirementScan, prior_states
    pass_finished = pyqtSignal(bool)  # True when the worker failed and the pass stopped early

    def __init__(self, jobs: list[ScanJob]) -> None:
        super().__init__()
        self._jobs = list(jobs)
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        failed = False
        try:
            for job in self._jobs:
                if self._stop_requested:
                    break
                scan = scan_documents(job.inputs)
                if self._stop_requested:
                    break
                self.job_scanned.emit(job.audit_id, scan, job.prior_states)
        except Exception as exc:
            # scan_documents never raises, so this is a defect. It is logged
            # once, by kind only, since a message could carry document text;
            # the next idle pass retries.
            logger.error("Requirements reconciler worker failed: %s", type(exc).__name__)
            failed = True
        finally:
            if not self._stop_requested:
                self.pass_finished.emit(failed)


class RequirementsReconciler(QObject):
    """Background reconciler owned by MainWindow (§7.2).

    can_write says whether the UI thread may write right now. It is false while
    a worker-thread transaction (an ingest's savepoint) holds the shared
    connection: a write then would join that transaction and roll back with
    it. A pass that would start then is deferred, and a result arriving then is
    dropped; resume_if_deferred picks both up once the operation ends, and the
    next idle pass would anyway.
    """

    pass_completed = pyqtSignal(object, bool)  # PassSummary, notify_toast
    audit_updated = pyqtSignal(int)

    def __init__(
        self,
        conn: sqlite3.Connection,
        vocabulary_provider: Callable[[], RequirementVocabulary],
        parent: QObject | None = None,
        can_write: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._vocabulary_provider = vocabulary_provider
        self._can_write = can_write or (lambda: True)
        self._thread: QThread | None = None
        self._worker: _ReconcilerWorker | None = None
        self._lingering_threads: list[tuple[QThread, _ReconcilerWorker]] = []
        self._rerun_pending: bool = False
        self._deferred: bool = False
        self._notify_on_complete: bool = False
        self._active_vocabulary: RequirementVocabulary = DEFAULT_VOCABULARY
        self._stopped: bool = False
        self._worker_failed: bool = False
        self._reset_counters()

    def _reset_counters(self) -> None:
        self._pass_jobs = 0
        self._pass_written = 0
        self._pass_dropped = 0
        self._pass_changed = 0
        self._pass_failed = 0

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def stale_scan_jobs(self, vocabulary: RequirementVocabulary | None = None) -> list[ScanJob]:
        vocab = vocabulary or self._vocabulary_provider()
        return stale_scan_jobs(self._conn, vocab)

    def write_scan_if_current(
        self,
        audit_id: int,
        scan: RequirementScan,
        vocabulary: RequirementVocabulary | None = None,
    ) -> bool:
        vocab = vocabulary or self._active_vocabulary or self._vocabulary_provider()
        return write_scan_if_current(self._conn, audit_id, scan, vocab)

    def run_pass(
        self,
        jobs: list[ScanJob] | None = None,
        vocabulary: RequirementVocabulary | None = None,
    ) -> PassSummary:
        vocab = vocabulary or self._vocabulary_provider()
        if jobs is None:
            jobs = stale_scan_jobs(self._conn, vocab)
        return run_pass(self._conn, jobs, vocab)

    def trigger(self, *, notify_toast: bool = False) -> None:
        """Requests a pass; one already running coalesces this into one more pass afterwards (§7.2)."""
        if self._stopped:
            return
        if notify_toast:
            self._notify_on_complete = True
        if self._thread is not None:
            self._rerun_pending = True
            return
        if not self._start_pass() and not self._deferred:
            self._finish_pass()

    def resume_if_deferred(self) -> None:
        """Runs the pass that a closed can_write gate deferred, or the results it dropped, if any."""
        if self._deferred and not self._stopped:
            self._deferred = False
            self.trigger()

    def _start_pass(self) -> bool:
        """Starts a pass on a worker thread; returns True iff one is now running.

        post: False with _deferred set when the gate is closed; False when no
              audit is stale; the counters accumulate across coalesced passes
        """
        if self._stopped:
            return False
        if not self._can_write():
            self._deferred = True
            return False
        self._deferred = False
        self._worker_failed = False
        self._active_vocabulary = self._vocabulary_provider()
        try:
            jobs = stale_scan_jobs(self._conn, self._active_vocabulary)
        except sqlite3.Error as exc:
            logger.warning("Requirements pass not started: %s", type(exc).__name__)
            return False
        self._pass_jobs += len(jobs)
        if not jobs:
            return False

        thread = QThread()
        worker = _ReconcilerWorker(jobs)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.job_scanned.connect(self._on_job_scanned)
        worker.pass_finished.connect(self._on_pass_finished)
        worker.pass_finished.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)

        self._thread = thread
        self._worker = worker
        thread.start()
        return True

    def _on_job_scanned(
        self,
        audit_id: int,
        scan: RequirementScan,
        prior_states: dict[RequirementKind, RequirementState] | None,
    ) -> None:
        if self._stopped:
            return
        if not self._can_write():
            self._pass_dropped += 1
            self._deferred = True
            return
        try:
            wrote = write_scan_if_current(self._conn, audit_id, scan, self._active_vocabulary)
        except sqlite3.Error as exc:
            self._pass_failed += 1
            logger.warning("Requirement scan for audit %s not written: %s", audit_id, type(exc).__name__)
            return
        if not wrote:
            self._pass_dropped += 1
            return
        self._pass_written += 1
        if prior_states != scan.states:
            self._pass_changed += 1
        self.audit_updated.emit(audit_id)

    def _on_pass_finished(self, failed: bool = False) -> None:
        if failed:
            self._worker_failed = True
            self._pass_failed += 1

    def _on_thread_finished(self) -> None:
        thread = self._thread
        worker = self._worker
        self._thread = None
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()

        if self._stopped:
            return

        rerun = self._rerun_pending and not self._worker_failed
        self._rerun_pending = False
        if rerun and self._start_pass():
            return
        self._finish_pass()

    def _finish_pass(self) -> None:
        """Reports the pass, and any passes coalesced into it, as one PassSummary.

        A toast request survives a deferral, so it is shown when the deferred
        pass actually runs rather than for the work done before it.
        """
        summary = PassSummary(
            jobs=self._pass_jobs,
            written=self._pass_written,
            dropped_as_stale=self._pass_dropped,
            changed_state=self._pass_changed,
            failed=self._pass_failed,
            stopped_early=self._worker_failed,
        )
        notify = self._notify_on_complete and not self._deferred
        if notify:
            self._notify_on_complete = False
        self._reset_counters()
        if summary.jobs:
            logger.info(
                "Requirements pass: %d jobs, %d written, %d dropped, %d changed state, %d failed%s",
                summary.jobs, summary.written, summary.dropped_as_stale, summary.changed_state,
                summary.failed, ", stopped early" if summary.stopped_early else "",
            )
        self.pass_completed.emit(summary, notify)

    def shutdown(self, timeout_ms: int = RENDER_SHUTDOWN_TIMEOUT_MS) -> bool:
        """Stops the reconciler thread before closing SQLite (§7.3).

        Mirrors MainWindow.shutdown_render_thread: sets stop flag, disconnects
        signals, quits, waits up to timeout_ms, logs warning and keeps refs
        alive on timeout, and never calls terminate().
        """
        self._stopped = True
        self._rerun_pending = False
        thread = self._thread
        worker = self._worker
        if thread is None:
            return True

        if worker is not None:
            worker.request_stop()
            for sig, slot in (
                (worker.job_scanned, self._on_job_scanned),
                (worker.pass_finished, self._on_pass_finished),
            ):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass

        try:
            thread.finished.disconnect(self._on_thread_finished)
        except (TypeError, RuntimeError):
            pass

        thread.quit()
        finished = thread.wait(timeout_ms)
        if not finished:
            logger.warning(
                "RequirementsReconciler thread did not finish within %dms; "
                "retaining references without calling terminate()",
                timeout_ms,
            )
            if worker is not None:
                self._lingering_threads.append((thread, worker))
            return False

        self._thread = None
        self._worker = None
        return True
