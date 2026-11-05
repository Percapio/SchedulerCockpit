"""Phase 54 section 7.2 and section 8: the reconciler's pass lifecycle, and writes on a shared connection.

Covers what the first-round tests left out: coalescing, the can_write gate that
keeps UI-thread writes out of an ingest's worker-thread transaction, stopping a
running pass within its bound, and the worker never touching the connection.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from cockpit.persistence.connection import open_connection
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.repositories.requirements import (
    AuditRequirementsRepository,
    set_xray_images_done,
)
from cockpit.persistence.schema import migrate
from cockpit.persistence.types import ActiveAuditDraft
from cockpit.services import requirements_reconciler as rr
from cockpit.services.requirements import (
    DEFAULT_VOCABULARY,
    AuditNotFoundForTick,
    RequirementKind,
    RequirementState,
    SaveImagesNotRequired,
    scan_documents,
)
from tests.services.test_phase54_requirements import _make_bom_xlsx, _make_docx


@pytest.fixture
def shop(tmp_path: Path):
    conn = open_connection(tmp_path / "test.db")
    migrate(conn, parser_registry=None)
    audits = AuditRepository(conn, AuditBomComponentRepository(conn), PdfComponentCoordRepository(conn))
    yield conn, audits, tmp_path
    conn.close()


def add_audit(conn, audits, tmp_path: Path, n: int, *, save_images: bool = True) -> int:
    eco_text = "X-ray is required. Save images." if save_images else "Hand solder J1."
    eco = _make_docx(tmp_path / f"eco{n}.docx", [eco_text], [])
    bom = _make_bom_xlsx(tmp_path / f"bom{n}.xlsx", [[1, "MPN-1", "U1", "BGA", "SMT"]])
    audit = audits.create(ActiveAuditDraft(
        part_number=f"ASSY-{n}", work_order_ref=f"WO-{n}", quantity=10,
        traveler_metadata={"process_clean": "No Clean"},
    ))
    conn.execute(
        "INSERT INTO source_files (audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (?, 'Notes', 'eco.docx', ?, ?, '2026-01-01T00:00:00'), "
        "       (?, 'BOM', 'bom.xlsx', ?, ?, '2026-01-01T00:00:00')",
        (audit.id, str(eco), f"eco{n}", audit.id, str(bom), f"bom{n}"),
    )
    return audit.id


def scanned_ids(conn) -> set[int]:
    return {row["audit_id"] for row in conn.execute("SELECT audit_id FROM audit_requirements").fetchall()}


# ---------- writes on the shared connection ----------

def test_writes_succeed_while_a_worker_transaction_is_open(shop):
    """An ingest holds SAVEPOINT ingest on a worker thread; a BEGIN IMMEDIATE inside it raised."""
    conn, audits, tmp_path = shop
    audit_id = add_audit(conn, audits, tmp_path, 1)
    (job,) = rr.stale_scan_jobs(conn, DEFAULT_VOCABULARY)
    scan = scan_documents(job.inputs)

    conn.execute("SAVEPOINT ingest")
    assert rr.write_scan_if_current(conn, audit_id, scan, DEFAULT_VOCABULARY) is True
    assert set_xray_images_done(conn, audit_id, True).is_ok
    conn.execute("RELEASE SAVEPOINT ingest")

    assert scanned_ids(conn) == {audit_id}
    assert audits.find_by_id(audit_id).xray_images_done is True


def test_a_tick_without_a_save_images_requirement_is_refused_and_writes_nothing(shop):
    conn, audits, tmp_path = shop
    audit_id = add_audit(conn, audits, tmp_path, 1, save_images=False)
    rr.run_pass(conn, rr.stale_scan_jobs(conn, DEFAULT_VOCABULARY), DEFAULT_VOCABULARY)
    summary = AuditRequirementsRepository(conn).find_summary(audit_id)
    assert summary.states[RequirementKind.XRAY_SAVE_IMAGES] == RequirementState.ABSENT

    refused = set_xray_images_done(conn, audit_id, True)
    assert refused.is_err and refused.error == SaveImagesNotRequired(audit_id=audit_id)
    assert audits.find_by_id(audit_id).xray_images_done is False

    gone = set_xray_images_done(conn, audit_id + 100, True)
    assert gone.is_err and gone.error == AuditNotFoundForTick(audit_id=audit_id + 100)


def test_the_tick_records_the_stored_task_and_unticking_clears_it(shop):
    conn, audits, tmp_path = shop
    audit_id = add_audit(conn, audits, tmp_path, 1)
    rr.run_pass(conn, rr.stale_scan_jobs(conn, DEFAULT_VOCABULARY), DEFAULT_VOCABULARY)
    task = AuditRequirementsRepository(conn).find_summary(audit_id).xray_task
    assert task

    ticked = set_xray_images_done(conn, audit_id, True)
    assert ticked.is_ok and ticked.value.task_at_tick == task
    assert audits.find_by_id(audit_id).xray_images_done_task == task

    cleared = set_xray_images_done(conn, audit_id, False)
    assert cleared.is_ok and cleared.value.task_at_tick is None
    audit = audits.find_by_id(audit_id)
    assert (audit.xray_images_done, audit.xray_images_done_task, audit.xray_images_done_at) == (False, None, None)


# ---------- the pass lifecycle ----------

class GatedScan:
    """Stands in for scan_documents on the worker, holding each call until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.worker_threads: set[int] = set()

    def __call__(self, inputs):
        self.worker_threads.add(threading.get_ident())
        self.started.set()
        self.release.wait(timeout=10)
        return scan_documents(inputs)


def make_reconciler(conn, **kwargs) -> rr.RequirementsReconciler:
    reconciler = rr.RequirementsReconciler(conn, lambda: DEFAULT_VOCABULARY, **kwargs)
    summaries: list[rr.PassSummary] = []
    reconciler.pass_completed.connect(lambda summary, notify: summaries.append(summary))
    reconciler.summaries = summaries
    return reconciler


def test_a_trigger_during_a_pass_is_coalesced_into_one_more_pass(shop, qtbot, monkeypatch):
    conn, audits, tmp_path = shop
    first = add_audit(conn, audits, tmp_path, 1)
    gate = GatedScan()
    monkeypatch.setattr(rr, "scan_documents", gate)
    reconciler = make_reconciler(conn)

    reconciler.trigger()
    assert gate.started.wait(5)
    second = add_audit(conn, audits, tmp_path, 2)
    reconciler.trigger()
    reconciler.trigger()
    gate.release.set()

    qtbot.waitUntil(lambda: bool(reconciler.summaries), timeout=10_000)
    assert reconciler.summaries == [rr.PassSummary(
        jobs=2, written=2, dropped_as_stale=0, changed_state=2, failed=0, stopped_early=False,
    )]
    assert scanned_ids(conn) == {first, second}
    assert reconciler.shutdown(timeout_ms=2_000)


def test_results_arriving_while_writes_are_blocked_are_deferred_then_written(shop, qtbot, monkeypatch):
    conn, audits, tmp_path = shop
    audit_id = add_audit(conn, audits, tmp_path, 1)
    gate = GatedScan()
    monkeypatch.setattr(rr, "scan_documents", gate)
    writes_allowed = [True]
    reconciler = make_reconciler(conn, can_write=lambda: writes_allowed[0])

    reconciler.trigger()
    assert gate.started.wait(5)
    writes_allowed[0] = False
    gate.release.set()
    qtbot.waitUntil(lambda: bool(reconciler.summaries), timeout=10_000)
    assert reconciler.summaries[0].dropped_as_stale == 1
    assert scanned_ids(conn) == set()

    writes_allowed[0] = True
    reconciler.resume_if_deferred()
    qtbot.waitUntil(lambda: len(reconciler.summaries) == 2, timeout=10_000)
    assert reconciler.summaries[1].written == 1
    assert scanned_ids(conn) == {audit_id}
    assert reconciler.shutdown(timeout_ms=2_000)


def test_a_pass_requested_while_writes_are_blocked_waits_and_keeps_its_toast(shop, qtbot):
    conn, audits, tmp_path = shop
    add_audit(conn, audits, tmp_path, 1)
    writes_allowed = [False]
    reconciler = rr.RequirementsReconciler(conn, lambda: DEFAULT_VOCABULARY,
                                           can_write=lambda: writes_allowed[0])
    completed: list[tuple[rr.PassSummary, bool]] = []
    reconciler.pass_completed.connect(lambda summary, notify: completed.append((summary, notify)))

    reconciler.trigger(notify_toast=True)
    qtbot.wait(50)
    assert completed == [] and not reconciler.is_running()

    writes_allowed[0] = True
    reconciler.resume_if_deferred()
    qtbot.waitUntil(lambda: bool(completed), timeout=10_000)
    summary, notify = completed[0]
    assert summary.written == 1 and notify is True
    assert reconciler.shutdown(timeout_ms=2_000)


def test_shutdown_is_bounded_never_terminates_and_drops_a_late_result(shop, qtbot, monkeypatch, caplog):
    conn, audits, tmp_path = shop
    add_audit(conn, audits, tmp_path, 1)
    gate = GatedScan()
    monkeypatch.setattr(rr, "scan_documents", gate)
    reconciler = make_reconciler(conn)
    reconciler.trigger()
    assert gate.started.wait(5)
    thread = reconciler._thread

    with caplog.at_level(logging.WARNING, logger="cockpit.services.requirements_reconciler"):
        assert reconciler.shutdown(timeout_ms=100) is False
    assert "did not finish" in caplog.text

    gate.release.set()
    assert thread.wait(5_000)
    qtbot.wait(50)
    assert scanned_ids(conn) == set()
    assert reconciler.summaries == []
    reconciler.trigger()
    assert not reconciler.is_running()


def test_the_worker_never_touches_the_connection(shop, qtbot, monkeypatch):
    conn, audits, tmp_path = shop
    add_audit(conn, audits, tmp_path, 1)
    add_audit(conn, audits, tmp_path, 2)
    gate = GatedScan()
    gate.release.set()
    monkeypatch.setattr(rr, "scan_documents", gate)

    ui_thread = threading.get_ident()
    connection_threads: set[int] = set()

    class RecordingConnection:
        def __getattr__(self, name):
            return getattr(conn, name)

        def cursor(self, *args):
            connection_threads.add(threading.get_ident())
            return conn.cursor(*args)

        def execute(self, *args):
            connection_threads.add(threading.get_ident())
            return conn.execute(*args)

    reconciler = make_reconciler(RecordingConnection())
    reconciler.trigger()
    qtbot.waitUntil(lambda: bool(reconciler.summaries), timeout=10_000)

    assert reconciler.summaries[0].written == 2
    assert gate.worker_threads and ui_thread not in gate.worker_threads
    assert connection_threads == {ui_thread}
    assert reconciler.shutdown(timeout_ms=2_000)
