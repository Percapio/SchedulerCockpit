"""Phase 54 section 9: the X-ray cell before a scan, the one-time column reset, and refreshes that keep your place."""

import logging

from PyQt6.QtCore import QSettings, Qt

from cockpit.services.requirements import RequirementState
from cockpit.ui.widgets.open_audit_picker import (
    _DEFAULT_COLUMN_WIDTHS,
    _HEADER_COLUMNS_KEY,
    _HEADER_STATE_KEY,
    Column,
    OpenAuditPicker,
)
from tests.ui.test_phase54_ui import _make_digest, _summary


def cell(picker: OpenAuditPicker, row: int, column: Column, role=Qt.ItemDataRole.DisplayRole):
    return picker.model.index(row, column).data(role)


def first_data_row(picker: OpenAuditPicker) -> int:
    for row in range(picker.model.rowCount()):
        data = picker.model.index(row, 0).data(Qt.ItemDataRole.UserRole)
        if data and data["kind"] == "data":
            return row
    raise AssertionError("no data row")


def test_a_not_yet_scanned_audit_shows_a_question_mark_in_every_requirement_column(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([_make_digest(requirements=None)])
    row = first_data_row(picker)

    assert cell(picker, row, Column.XRAY) == "?"
    assert cell(picker, row, Column.XRAY, Qt.ItemDataRole.CheckStateRole) is None
    assert cell(picker, row, Column.AQUANOX) == "?"
    assert cell(picker, row, Column.PRESS_FIT) == "?"
    assert "Not scanned yet" in cell(picker, row, Column.XRAY, Qt.ItemDataRole.ToolTipRole)


def test_a_scanned_audit_with_nothing_required_shows_blank_cells(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([_make_digest(requirements=_summary(xr=RequirementState.ABSENT))])
    row = first_data_row(picker)
    assert cell(picker, row, Column.XRAY) == ""
    assert cell(picker, row, Column.AQUANOX) == ""


def test_a_layout_saved_with_another_column_set_resets_once(qtbot, tmp_path, caplog):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    before = OpenAuditPicker(settings=settings)
    qtbot.addWidget(before)
    before.table_view.horizontalHeader().resizeSection(Column.AQUANOX, 222)
    assert settings.value(_HEADER_STATE_KEY) is not None

    # As an install from before Phase 54 left it: a valid state, no column count.
    settings.remove(_HEADER_COLUMNS_KEY)
    with caplog.at_level(logging.WARNING):
        upgraded = OpenAuditPicker(settings=settings)
        qtbot.addWidget(upgraded)
    assert any("header state failed to restore" in r.message for r in caplog.records)
    assert upgraded.table_view.horizontalHeader().sectionSize(Column.AQUANOX) == _DEFAULT_COLUMN_WIDTHS[Column.AQUANOX]
    assert int(settings.value(_HEADER_COLUMNS_KEY)) == len(Column)

    upgraded.table_view.horizontalHeader().resizeSection(Column.AQUANOX, 77)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        later = OpenAuditPicker(settings=settings)
        qtbot.addWidget(later)
    assert not any("header state failed to restore" in r.message for r in caplog.records)
    assert later.table_view.horizontalHeader().sectionSize(Column.AQUANOX) == 77


def test_a_refresh_you_didnt_ask_for_keeps_your_place(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.resize(900, 300)
    picker.show()
    digests = [_make_digest(audit_id=n, part_number=f"ASSY-{n}") for n in range(1, 120)]
    picker.populate(digests)
    scroll_bar = picker.table_view.verticalScrollBar()
    qtbot.waitUntil(lambda: scroll_bar.maximum() > 0, timeout=5_000)
    scroll_bar.setValue(scroll_bar.maximum() // 2)
    place = scroll_bar.value()

    picker.populate(digests, keep_scroll=True)
    assert scroll_bar.value() == place

    picker.populate(digests)
    assert scroll_bar.value() == 0
