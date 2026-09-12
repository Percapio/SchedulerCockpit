"""End-to-end cover for the audit-load path that the hang exposed as untested.

tests/ui/test_ingestion.py::test_successful_ingestion skips when no sample
trio is present, which it is not in this tree. The result was that 496 tests
passed while every audit load raised AttributeError: nothing in the suite ever
opened an audit view.

These tests build a real MainWindow over a real bootstrap and drive the two
paths that reach AuditView.load -- post-ingest and picker -- with an audit
inserted straight into the database, so no sample workbooks are needed.
"""

import pathlib

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from cockpit.ui.bootstrap import bootstrap
from cockpit.ui.config import resolve_config
from cockpit.ui.main_window import MainWindow
from cockpit.ui.theme import ThemeLoader
from cockpit.ui.workers.ingestion_worker import AuditSummary


@pytest.fixture
def app_config(tmp_path, monkeypatch):
    root = tmp_path / "cockpit_data"
    root.mkdir()
    monkeypatch.setenv("COCKPIT_APP_DATA", str(root))
    return resolve_config(root / "v1")


@pytest.fixture
def bootstrapped_app(app_config):
    return bootstrap(app_config)


@pytest.fixture
def seeded_audit(bootstrapped_app):
    """One audit with a BOM source file and four component rows."""
    conn = bootstrapped_app.conn
    conn.execute(
        "INSERT INTO active_audits "
        "(id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (1, 'ASSY-A', 'WO-1', '', 10, 'Not Clear', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source_files "
        "(id, audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at) "
        "VALUES (10, 1, 'BOM', 'bom.xlsx', '/tmp/bom.xlsx', 'h1', '2026-01-01T00:00:00')"
    )
    conn.executemany(
        "INSERT INTO audit_bom_components "
        "(source_file_id, component_mpn, ref_des, mount_type, description, find_number) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (10, "RC0805FR-0710KL", "R1", "S", "RES 10K", "1"),
            (10, " rc0805fr-0710kl ", "R2", "S", "RES 10K", "2"),
            (10, "SN74LVC1G08DBVR", "U1", "S", "AND GATE", "3"),
            (10, "CAP NBSP", "C9", "S", "non-ASCII", "4"),
        ],
    )
    return 1


@pytest.fixture
def make_window(qtbot, bootstrapped_app, tmp_path):
    """MainWindow factory that shuts the render thread down afterwards.

    MainWindow.__init__ starts the render QThread unconditionally. Letting the
    window be collected with that thread running makes Qt call qFatal, which
    aborts the whole pytest process rather than failing a test.
    """
    windows = []

    def build(library_enabled=None):
        ui_dir = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui"
        theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
        settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)

        controller = None
        if library_enabled is not None:
            from cockpit.settings.mpn_library import MpnLibrarySettingsController

            controller = MpnLibrarySettingsController(settings)
            controller.set_enabled(library_enabled)

        window = MainWindow(
            QApplication.instance(),
            bootstrapped_app,
            bootstrapped_app.audit_read_svc,
            bootstrapped_app.checklist_svc,
            bootstrapped_app.split_svc,
            bootstrapped_app.completion_svc,
            bootstrapped_app.layout_query_svc,
            bootstrapped_app.pdf_renderer,
            bootstrapped_app.holiday_svc,
            theme=theme,
            settings=settings,
            mpn_library_controller=controller,
        )
        qtbot.addWidget(window)
        windows.append(window)
        return window

    yield build

    for window in windows:
        try:
            if window._library_module is not None:
                window._library_module.teardown()
                window._library_module = None
        except Exception:
            pass
        window.shutdown_render_thread(timeout_ms=2000)


def test_audit_view_loads_with_library_disabled(make_window, bootstrapped_app, seeded_audit):
    window = make_window(library_enabled=False)
    window._audit_view.load(seeded_audit)
    assert window._audit_view.current_audit_id() == seeded_audit


def test_audit_view_loads_with_library_enabled(make_window, bootstrapped_app, seeded_audit):
    """Covers the FrozenInstanceError and the missing repository method together."""
    window = make_window(library_enabled=True)
    window._sync_mpn_library_state()

    assert window._library_module is not None, "library module was not constructed"
    window._audit_view.load(seeded_audit)
    assert window._audit_view.current_audit_id() == seeded_audit


def test_library_db_lands_beside_local_audit_db_not_under_uploads(make_window, bootstrapped_app, seeded_audit):
    """Phase 47a section 4.1: under uploads/ the startup reaper would delete it."""
    window = make_window(library_enabled=True)
    window._sync_mpn_library_state()

    config = bootstrapped_app.config
    expected = config.app_data_root / "parts_library.db"
    assert expected.exists()
    assert expected.parent == config.db_path.parent
    assert config.file_storage_root not in expected.parents


def test_ingest_success_path_loads_the_audit_and_switches_view(make_window, bootstrapped_app, seeded_audit):
    """The exact slot whose exception left the progress view up forever."""
    window = make_window(library_enabled=True)

    window._on_ingest_succeeded(
        AuditSummary(
            audit_id=seeded_audit,
            part_number="ASSY-A",
            work_order_ref="WO-1",
            tht_item_count=0,
            eco_item_count=0,
        )
    )

    assert window.stacked.currentWidget() == window._audit_view
    assert window._audit_view.current_audit_id() == seeded_audit


def test_library_segment_is_offered_after_ingest_not_only_from_the_picker(make_window, bootstrapped_app, seeded_audit):
    from cockpit.ui.widgets.center_pager import CenterPage

    window = make_window(library_enabled=True)
    window._on_ingest_succeeded(
        AuditSummary(seeded_audit, "ASSY-A", "WO-1", 0, 0)
    )

    selector = window._audit_view._center_pager._selector
    assert CenterPage.LIBRARY in selector._buttons


def test_toggling_the_library_off_keeps_the_database_and_its_rows(make_window, bootstrapped_app, seeded_audit):
    """Phase 47a section 7.2: toggle-off must never destroy hand-entered data."""
    from cockpit.persistence.clock import utcnow

    window = make_window(library_enabled=True)
    window._sync_mpn_library_state()

    lines = bootstrapped_app.audit_bom_component_repo.list_bom_lines_for_audit(seeded_audit)
    window._library_module.repository.observe_bom(lines, utcnow)

    db_path = bootstrapped_app.config.app_data_root / "parts_library.db"
    cur = window._library_module.ui_conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM library_part")
    before = cur.fetchone()["n"]
    assert before > 0

    window._mpn_library_controller.set_enabled(False)
    window._sync_mpn_library_state()
    assert window._library_module is None
    assert db_path.exists()

    window._mpn_library_controller.set_enabled(True)
    window._sync_mpn_library_state()
    cur = window._library_module.ui_conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM library_part")
    assert cur.fetchone()["n"] == before


def test_observe_dedupes_dirty_spellings_and_flags_non_ascii(make_window, bootstrapped_app, seeded_audit):
    from cockpit.persistence.clock import utcnow

    window = make_window(library_enabled=True)
    window._sync_mpn_library_state()

    lines = bootstrapped_app.audit_bom_component_repo.list_bom_lines_for_audit(seeded_audit)
    window._library_module.repository.observe_bom(lines, utcnow)

    cur = window._library_module.ui_conn.cursor()
    cur.execute("SELECT mpn_key, resolution_status FROM library_part ORDER BY mpn_key")
    rows = {r["mpn_key"]: r["resolution_status"] for r in cur.fetchall()}

    # Four BOM lines, two of which are the same part spelled differently.
    assert rows["RC0805FR-0710KL"] == "UNRESOLVED"
    assert " rc0805fr-0710kl " not in rows
    assert rows["SN74LVC1G08DBVR"] == "UNRESOLVED"
    assert rows["CAP NBSP"] == "UNKEYABLE"
