"""Phase 50 sections 2.2-2.4 and 7.4-7.5 — the Details modal."""

from datetime import date

import pytest
from PyQt6.QtCore import Qt, QDate

from cockpit.services.release import ReleaseFormData
from cockpit.ui.widgets.release_dialog import (
    DetailsOutcome,
    ReleaseDialog,
    resolve_ship_date,
)


def form_data(ship_date="2026-10-01"):
    return ReleaseFormData(
        assembly_number="B123456",
        quantity=10,
        lead_time_days=5,
        repeat="ROWC 123",
        assembly_modifier=None,
        itar_display="",
        process_clean="CLEAN",
        class_display="Class 3",
        process="LEAD FREE",
        ship_date=ship_date,
        turn_note="",
        floor_notes="",
        shortages_notes="",
        pcb_clear="",
        setup_first_side="",
        program_in_kit=False,
        folder_in_kit=False,
    )


@pytest.fixture
def dialog(qtbot):
    d = ReleaseDialog(form_data(), "Not Clear")
    qtbot.addWidget(d)
    return d


# --- section 7.4, resolve_ship_date -----------------------------------------

def test_blank_clears():
    assert resolve_ship_date("2026-10-01", blank_requested=True) is None


def test_valid_iso_parses():
    assert resolve_ship_date("2026-10-01", blank_requested=False) == date(2026, 10, 1)


def test_empty_non_blank_is_none():
    assert resolve_ship_date("", blank_requested=False) is None


def test_unparseable_non_blank_raises():
    """The swallow this replaces wrote NULL and cleared the operator's date."""
    with pytest.raises(ValueError):
        resolve_ship_date("not-a-date", blank_requested=False)


# --- section 7.4, has_persistable_change ------------------------------------

def test_no_change_on_open(dialog):
    assert dialog.has_persistable_change() is False
    assert dialog.update_btn.isVisible() is False


def test_status_change_makes_update_appear(dialog, qtbot):
    dialog.show()
    assert dialog.update_btn.isVisible() is False

    dialog.status_combo.setCurrentText("SMT")

    assert dialog.has_persistable_change() is True
    assert dialog.update_btn.isVisible() is True


def test_reverting_the_status_hides_update_again(dialog):
    dialog.show()
    original = dialog.status_combo.currentText()
    dialog.status_combo.setCurrentText("SMT")
    assert dialog.update_btn.isVisible() is True

    dialog.status_combo.setCurrentText(original)

    assert dialog.update_btn.isVisible() is False


def test_ship_date_change_makes_update_appear(dialog):
    dialog.show()
    dialog.ship_date_input.setDate(QDate(2026, 12, 25))
    assert dialog.has_persistable_change() is True


def test_blank_toggle_makes_update_appear(dialog):
    dialog.show()
    dialog.ship_date_blank_check.setChecked(True)
    assert dialog.has_persistable_change() is True


@pytest.mark.parametrize(
    "attr,value",
    [
        ("qty_input", "9999"),
        ("assembly_input", "B999999"),
        ("clean_input", "NO CLEAN"),
        ("class_input", "Class 2"),
        ("process_input", "LEADED"),
        ("turn_note_input", "HOT"),
        ("floor_notes_input", "note"),
        ("shortages_notes_input", "short"),
        ("lead_time_input", "42"),
    ],
)
def test_print_payload_edits_never_enable_update(dialog, attr, value):
    dialog.show()
    getattr(dialog, attr).setText(value)
    assert dialog.has_persistable_change() is False
    assert dialog.update_btn.isVisible() is False


def test_checkbox_payload_edits_never_enable_update(dialog):
    dialog.show()
    dialog.program_check.setChecked(True)
    dialog.folder_check.setChecked(True)
    assert dialog.has_persistable_change() is False


# --- section 2.2, outcomes and closing --------------------------------------

def test_release_is_always_available(dialog):
    dialog.show()
    assert dialog.release_btn.isVisible() is True
    assert dialog.release_btn.isEnabled() is True


def test_update_does_not_close_and_asks_to_be_committed(dialog, qtbot):
    """Update is an apply, not an OK."""
    dialog.show()
    dialog.status_combo.setCurrentText("SMT")

    with qtbot.waitSignal(dialog.update_requested):
        dialog.update_btn.click()

    assert dialog.isVisible() is True
    assert dialog.result() == 0


def test_release_accepts_and_reports_release(dialog):
    dialog.release_btn.click()
    assert dialog.result() == int(ReleaseDialog.DialogCode.Accepted)
    assert dialog.outcome() == DetailsOutcome.RELEASE


def test_close_rejects_and_reports_closed(dialog):
    dialog.close_btn.click()
    assert dialog.result() == int(ReleaseDialog.DialogCode.Rejected)
    assert dialog.outcome() == DetailsOutcome.CLOSED


def test_third_button_is_named_close(dialog):
    """It has never undone anything, and now Update commits without closing."""
    assert dialog.close_btn.text() == "Close"


# --- section 2.2 and 2.4, the in-place commit -------------------------------

def test_commit_rebases_and_hides_update(dialog):
    dialog.show()
    dialog.status_combo.setCurrentText("SMT")
    assert dialog.update_btn.isVisible() is True

    dialog.mark_committed("SMT", dialog._current_ship_date_text())

    assert dialog.has_persistable_change() is False
    assert dialog.update_btn.isVisible() is False
    assert dialog.status_lbl.text() == "Saved."


def test_editing_after_a_commit_brings_update_back_and_clears_the_line(dialog):
    dialog.show()
    dialog.status_combo.setCurrentText("SMT")
    dialog.mark_committed("SMT", dialog._current_ship_date_text())
    assert dialog.status_lbl.text() == "Saved."

    dialog.status_combo.setCurrentText("THT")

    assert dialog.update_btn.isVisible() is True
    assert dialog.status_lbl.text() == ""


def test_committing_twice_hides_update_again(dialog):
    dialog.show()
    dialog.status_combo.setCurrentText("SMT")
    dialog.mark_committed("SMT", dialog._current_ship_date_text())
    dialog.status_combo.setCurrentText("THT")
    dialog.mark_committed("THT", dialog._current_ship_date_text())

    assert dialog.update_btn.isVisible() is False
    assert dialog.status_lbl.text() == "Saved."


def test_a_failed_commit_keeps_update_available_to_retry(dialog):
    dialog.show()
    dialog.status_combo.setCurrentText("SMT")

    dialog.mark_commit_failed("Audit 7 not found")

    assert dialog.update_btn.isVisible() is True
    assert dialog.status_lbl.text() == "Audit 7 not found"
    assert dialog.has_persistable_change() is True


def test_a_ship_date_commit_rebases_too(dialog):
    dialog.show()
    dialog.ship_date_blank_check.setChecked(True)
    assert dialog.update_btn.isVisible() is True

    dialog.mark_committed(dialog.status_combo.currentText(), "")

    assert dialog.update_btn.isVisible() is False


def test_window_is_titled_details(dialog):
    assert dialog.windowTitle() == "Details"


def test_notice_line_is_present(dialog):
    from PyQt6.QtWidgets import QLabel

    texts = [w.text() for w in dialog.findChildren(QLabel)]
    assert any("printed form only" in t for t in texts)
    assert any("Workflow Status and Ship Date are saved" in t for t in texts)


def test_print_payload_edits_still_reach_get_result(dialog):
    """Editable for the printout is the whole point of keeping them enabled."""
    dialog.qty_input.setText("4321")
    data, _status = dialog.get_result()
    assert data.quantity == 4321
