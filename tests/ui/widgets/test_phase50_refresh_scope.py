"""Phase 50 section 2.6 — what a Details commit refreshes, and what it must not.

AuditView.load costs ~126 ms on a 374-line BOM, essentially all of it in
_bom_panel.load, and requests a fresh canvas raster on top. Neither the BOM
panel nor the audit view renders status or ship date, so a commit that changed
only those two columns must not pay any of it.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cockpit.persistence.types import AuditStatus
from cockpit.ui.widgets.audit_actions_bar import AuditActionsBar
from cockpit.ui.widgets.audit_view import AuditView


def make_view(status="Not Clear", ship_date="2026-10-01"):
    return SimpleNamespace(
        audit_id=7,
        part_number="B123456",
        has_pdf=True,
        has_secondary_pdf=False,
        status=status,
        ship_date=ship_date,
        quantity=10,
        traveler_metadata={},
    )


@pytest.fixture
def bar(qtbot):
    release_service = MagicMock()
    release_service.build_defaults.return_value = _form_data()
    b = AuditActionsBar(
        MagicMock(), MagicMock(), MagicMock(), release_service, MagicMock()
    )
    qtbot.addWidget(b)
    session = MagicMock()
    session.current_view.return_value = make_view()
    b.bind(session)
    return b


def _form_data():
    from cockpit.services.release import ReleaseFormData

    return ReleaseFormData(
        assembly_number="B123456", quantity=10, lead_time_days=5, repeat="NEW",
        assembly_modifier=None, itar_display="", process_clean="CLEAN",
        class_display="Class 3", process="LEAD FREE", ship_date="2026-10-01",
        turn_note="", floor_notes="", shortages_notes="", pcb_clear="",
        setup_first_side="", program_in_kit=False, folder_in_kit=False,
    )


def make_dialog(qtbot, status="Not Clear"):
    from cockpit.ui.widgets.release_dialog import ReleaseDialog

    d = ReleaseDialog(_form_data(), status)
    qtbot.addWidget(d)
    return d


# --- the narrow signal ------------------------------------------------------

def test_commit_emits_audit_fields_changed_not_reload_requested(bar, qtbot):
    dialog = make_dialog(qtbot)
    dialog.status_combo.setCurrentText("SMT")

    reloads = []
    bar.reload_requested.connect(reloads.append)

    with qtbot.waitSignal(bar.audit_fields_changed) as blocker:
        assert bar._commit_details(dialog, 7) is True

    assert blocker.args == [7]
    assert reloads == [], "a scalar commit must not trigger a full reload"


def test_commit_persists_the_status_and_ship_date(bar, qtbot):
    dialog = make_dialog(qtbot)
    dialog.status_combo.setCurrentText("SMT")

    bar._commit_details(dialog, 7)

    bar._release_service.persist_release.assert_called_once()
    args = bar._release_service.persist_release.call_args[0]
    assert args[0] == 7
    assert args[1] == AuditStatus("SMT")


def test_commit_rebases_the_dialog(bar, qtbot):
    dialog = make_dialog(qtbot)
    dialog.show()
    dialog.status_combo.setCurrentText("SMT")
    assert dialog.update_btn.isVisible() is True

    bar._commit_details(dialog, 7)

    assert dialog.update_btn.isVisible() is False
    assert dialog.status_lbl.text() == "Saved."


def test_a_failed_commit_reports_inline_and_emits_nothing(bar, qtbot):
    dialog = make_dialog(qtbot)
    dialog.show()
    dialog.status_combo.setCurrentText("SMT")
    bar._release_service.persist_release.side_effect = RuntimeError("audit vanished")

    emitted = []
    bar.audit_fields_changed.connect(emitted.append)

    assert bar._commit_details(dialog, 7) is False

    assert emitted == []
    assert "audit vanished" in dialog.status_lbl.text()
    assert dialog.update_btn.isVisible() is True


def test_quiet_commit_routes_failures_to_the_error_path(bar, qtbot):
    """The Release path has no open modal to report into."""
    dialog = make_dialog(qtbot)
    bar._release_service.persist_release.side_effect = RuntimeError("nope")

    errors = []
    bar.error_occurred.connect(errors.append)

    assert bar._commit_details(dialog, 7, quiet=True) is False

    assert len(errors) == 1
    assert dialog.status_lbl.text() == ""


# --- the view-side handler --------------------------------------------------

def test_refresh_audit_fields_touches_neither_the_bom_panel_nor_the_canvas(qtbot):
    view = AuditView.__new__(AuditView)
    view._session = MagicMock()
    view._bom_panel = MagicMock()
    view._center_pager = MagicMock()
    view._layout_canvas = MagicMock()
    view._coordinator = MagicMock()

    AuditView.refresh_audit_fields(view, 7)

    view._session.reload.assert_called_once_with()
    view._bom_panel.load.assert_not_called()
    view._center_pager.load.assert_not_called()
    view._layout_canvas.reload.assert_not_called()


def test_full_reload_is_still_available_for_the_paths_that_need_it(qtbot):
    """Split and the drawing actions genuinely change BOM rows and drawings."""
    view = AuditView.__new__(AuditView)
    view._session = MagicMock()
    view._bom_panel = MagicMock()
    view._center_pager = MagicMock()
    view._coordinator = MagicMock()
    view._bom_component_repo = None
    view.unload = MagicMock()

    AuditView.load(view, 7)

    view._bom_panel.load.assert_called_once_with(7)
    view._center_pager.load.assert_called_once()
