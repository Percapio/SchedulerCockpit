"""Regression tests for the post-Phase-47a hang on the first job upload.

Four defects combined into one symptom: the progress view sat on the last
completed stage forever, the drop area stayed disabled, and every later
operation was refused by the _operation_in_flight guard.

  1. AuditView.load called AuditBomComponentRepository.get_components_for_audit,
     which does not exist, so every audit load raised AttributeError.
  2. The crash reporter writes to sys.stderr, which is None in a PyInstaller
     windowed build, so the reporter raised inside its own handler. The
     exception was swallowed at the PyQt boundary, the GUI dialog sink was
     never reached, and the remaining slots on succeeded_signal -- including
     QThread.quit -- were never invoked. That is what made it a silent hang in
     the bundled exe but a visible traceback from source.
  3. BootstrappedApp is a frozen dataclass, and _sync_mpn_library_state
     assigned to its library_module field.
  4. mpn_library.module imported Clock from cockpit.persistence.clock, which
     does not define it, so the module could not be imported at all.
"""

import inspect
import pathlib
import sqlite3
import sys

import pytest

from cockpit.persistence.connection import hydrating_row_factory
from cockpit.persistence.schema import migrate
from cockpit.persistence.repositories.bom_components import (
    AuditBomComponentRepository,
    PersistedBomLine,
)
from cockpit.protocols import ParserRegistry


# --------------------------------------------------------------------------
# Defect 1 -- the missing repository method
# --------------------------------------------------------------------------

@pytest.fixture
def bom_repo(tmp_path):
    conn = sqlite3.connect(tmp_path / "stall.db", isolation_level=None)
    conn.row_factory = hydrating_row_factory

    class DummyParser:
        def parse(self, path):
            return None

    migrate(conn, ParserRegistry(DummyParser(), None, None, None, None))
    repo = AuditBomComponentRepository(conn)

    for audit_id, part in ((1, "ASSY-A"), (2, "ASSY-B")):
        conn.execute(
            "INSERT INTO active_audits "
            "(id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
            "VALUES (?, ?, ?, '', 10, 'Not Clear', '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (audit_id, part, f"WO-{audit_id}"),
        )
        conn.execute(
            "INSERT INTO source_files "
            "(id, audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
            "VALUES (?, ?, 'BOM', 'bom.xlsx', ?, ?, '2026-01-01T00:00:00')",
            (audit_id * 10, audit_id, f"/tmp/bom{audit_id}.xlsx", f"hash{audit_id}"),
        )

    conn.executemany(
        "INSERT INTO audit_bom_components "
        "(source_file_id, component_mpn, ref_des, mount_type, description, find_number) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (10, "RC0805FR-0710KL", "R1", "S", "RES 10K", "1"),
            (10, "RC0805FR-0710KL", "R2", "S", "RES 10K", "1"),
            (10, "SN74LVC1G08DBVR", "U1", "S", "AND GATE", "2"),
            (20, "GRM188R71H104KA", "C1", "S", "CAP 100N", "1"),
        ],
    )
    return repo


def test_the_method_auditview_calls_actually_exists(bom_repo):
    """The proximate cause: AuditView named a method the repository never had."""
    assert hasattr(bom_repo, "list_bom_lines_for_audit")


def test_audit_view_calls_only_real_repository_methods():
    """Guard against the same class of typo returning on this seam.

    AuditView reaches the repository through one helper. Every attribute it
    pulls off that repository must exist on the real class.
    """
    from cockpit.ui.widgets.audit_view import AuditView

    source = inspect.getsource(AuditView._library_bom_lines)
    called = {
        line.split("self._bom_component_repo.")[1].split("(")[0]
        for line in source.splitlines()
        if "self._bom_component_repo." in line and "(" in line
    }
    assert called, "helper no longer reaches the repository; update this test"
    for name in called:
        assert hasattr(AuditBomComponentRepository, name), (
            f"AuditView calls {name}, which AuditBomComponentRepository lacks"
        )


def test_list_bom_lines_for_audit_returns_persisted_bom_lines(bom_repo):
    lines = bom_repo.list_bom_lines_for_audit(1)

    assert all(isinstance(line, PersistedBomLine) for line in lines)
    # observe_bom reads line.component_mpn, so that attribute is the contract.
    assert {line.component_mpn for line in lines} == {
        "RC0805FR-0710KL",
        "SN74LVC1G08DBVR",
    }
    assert all(line.audit_id == 1 for line in lines)


def test_list_bom_lines_for_audit_does_not_leak_other_audits(bom_repo):
    assert {line.component_mpn for line in bom_repo.list_bom_lines_for_audit(2)} == {
        "GRM188R71H104KA"
    }


def test_list_bom_lines_for_audit_unknown_audit_is_empty_not_an_error(bom_repo):
    assert bom_repo.list_bom_lines_for_audit(999) == []


def test_library_bom_lines_never_raises_into_the_audit_load_path():
    """A library-only read must not abort load() before the canvas is loaded."""
    from cockpit.ui.widgets.audit_view import AuditView

    class ExplodingRepo:
        def list_bom_lines_for_audit(self, audit_id):
            raise sqlite3.OperationalError("database is locked")

    view = AuditView.__new__(AuditView)
    view._bom_component_repo = ExplodingRepo()
    assert view._library_bom_lines(1) == []

    view._bom_component_repo = None
    assert view._library_bom_lines(1) == []


# --------------------------------------------------------------------------
# Defect 2 -- the crash reporter in a windowed frozen build
# --------------------------------------------------------------------------

class _RecordingSink:
    """Stands in for GuiDialogCrashSink, the sink that was never reached."""

    def __init__(self):
        self.seen = []

    def emit(self, report):
        self.seen.append(report.exception_class)


@pytest.fixture
def stderrless_reporter(tmp_path, monkeypatch):
    from cockpit._build_info import get_build_info
    from cockpit.ui import crash_reporter
    from cockpit.ui.crash_reporter import (
        install_crash_reporter,
        LocalFileCrashSink,
        StderrCrashSink,
    )

    crash_dir = tmp_path / "crash_reports"
    later_sink = _RecordingSink()

    # _installed_chain is a module global that install_crash_reporter sets once
    # and never clears, so it has to be saved and reset here or the chain
    # assertion in tests/ui/test_crash_reporter.py fails depending on run order.
    original_chain = crash_reporter._installed_chain
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    crash_reporter._installed_chain = None

    install_crash_reporter(
        crash_dir=crash_dir,
        build_info=get_build_info(),
        sinks=(LocalFileCrashSink(crash_dir), StderrCrashSink(), later_sink),
    )
    hook = sys.excepthook

    # Exactly what PyInstaller gives a console=False build.
    monkeypatch.setattr(sys, "stderr", None)
    try:
        yield hook, later_sink, crash_dir
    finally:
        crash_reporter._installed_chain = original_chain


def _fire(hook):
    try:
        raise AttributeError("no attribute 'get_components_for_audit'")
    except AttributeError:
        hook(*sys.exc_info())


def test_excepthook_survives_stderr_being_none(stderrless_reporter):
    hook, _, _ = stderrless_reporter
    _fire(hook)  # must not raise


def test_sinks_after_the_stderr_sink_still_run(stderrless_reporter):
    hook, later_sink, _ = stderrless_reporter
    _fire(hook)
    assert later_sink.seen == ["AttributeError"], (
        "a raising StderrCrashSink aborted the sink loop, so the operator saw nothing"
    )


def test_crash_report_is_still_written_without_stderr(stderrless_reporter):
    hook, _, crash_dir = stderrless_reporter
    _fire(hook)
    assert len(list(crash_dir.glob("*.json"))) == 1


def test_write_stderr_tolerates_none_and_broken_streams():
    from cockpit.ui.crash_reporter import write_stderr

    real = sys.stderr

    class Broken:
        def write(self, _):
            raise OSError("stream closed")

    try:
        sys.stderr = None
        write_stderr("none stream\n")
        sys.stderr = Broken()
        write_stderr("broken stream\n")
    finally:
        sys.stderr = real


def test_no_bare_stderr_writes_remain_in_crash_reporter():
    """Every write in this module runs inside an exception handler."""
    from cockpit.ui import crash_reporter

    source = pathlib.Path(inspect.getfile(crash_reporter)).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "sys.stderr.write(" in line and not line.strip().startswith("#")
        and "sys.stderr.write raises" not in line
    ]
    assert offenders == [], offenders


# --------------------------------------------------------------------------
# Defect 3 -- the frozen dataclass
# --------------------------------------------------------------------------

def test_bootstrapped_app_is_frozen_so_the_module_is_held_on_main_window():
    """Documents why MainWindow owns _library_module rather than mutating bootstrap."""
    import dataclasses
    from cockpit.ui.bootstrap import BootstrappedApp

    assert dataclasses.fields(BootstrappedApp)
    assert BootstrappedApp.__dataclass_params__.frozen

    from cockpit.ui.main_window import MainWindow

    source = inspect.getsource(MainWindow._sync_mpn_library_state)
    assert "self._bootstrapped.library_module =" not in source, (
        "assigning to a frozen dataclass field raises FrozenInstanceError"
    )
    assert "self._library_module" in source


# --------------------------------------------------------------------------
# Defect 4 -- the unimportable module
# --------------------------------------------------------------------------

def test_every_mpn_library_module_imports():
    """module.py imported a name that does not exist, so nothing could load it."""
    import importlib

    for name in (
        "cockpit.services.mpn_library.module",
        "cockpit.services.mpn_library.connection",
        "cockpit.services.mpn_library.repository",
        "cockpit.services.mpn_library.registry",
        "cockpit.services.mpn_library.normalisation",
        "cockpit.services.mpn_library.schema",
        "cockpit.services.mpn_library.types",
        "cockpit.ui.widgets.library_segment",
        "cockpit.ui.widgets.library_grid",
    ):
        importlib.import_module(name)


# --------------------------------------------------------------------------
# The symptom -- terminal cleanup must be unconditional
# --------------------------------------------------------------------------

def test_ingest_cleanup_is_connected_before_the_business_slots():
    """Qt stops an emission when a slot raises, so cleanup must be connected first.

    With cleanup last, an exception in _on_ingest_succeeded left the thread
    running, _operation_in_flight true and the progress view on screen.
    """
    from cockpit.ui.main_window import MainWindow

    source = inspect.getsource(MainWindow._on_drop_received)
    quit_at = source.index("succeeded_signal.connect(self._thread.quit)")
    handler_at = source.index("succeeded_signal.connect(self._on_ingest_succeeded)")
    assert quit_at < handler_at, (
        "QThread.quit must be connected before _on_ingest_succeeded"
    )
