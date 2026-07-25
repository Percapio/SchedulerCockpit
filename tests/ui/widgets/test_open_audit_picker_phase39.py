import pytest
from datetime import datetime, timezone, date
from PyQt6.QtCore import Qt, QSettings, QEvent, QPoint, QRect
from PyQt6.QtGui import QMouseEvent, QKeyEvent
from cockpit.ui.widgets.open_audit_picker import OpenAuditPicker, Column, RowKind, _DEFAULT_COLUMN_WIDTHS
from cockpit.services.views import OpenAuditDigest
from cockpit.persistence.types import AuditStatus
from cockpit.services.date_urgency import DateUrgency
from cockpit.ui import facelift


def create_digest(
    audit_id=1,
    status=AuditStatus.NOT_CLEAR,
    start_by=None,
    ship_date=None,
    start_by_urgency=DateUrgency.COMFORTABLE,
    ship_urgency=DateUrgency.COMFORTABLE,
    is_labeled=False,
    are_photos_uploaded=False
):
    return OpenAuditDigest(
        audit_id=audit_id,
        part_number="PN-39",
        work_order_ref="WO-39",
        split_suffix="",
        quantity=10,
        status=status,
        updated_at=datetime.now(timezone.utc),
        date_ingested=datetime.now(timezone.utc),
        ship_date=ship_date,
        lead_time_days=None,
        repeat="NEW",
        classification="Non-ITAR",
        assembly_class=2,
        process=None,
        feeder_setuptime=1.0,
        smt_runtime=2.0,
        tht_runtime=3.0,
        aoi_runtime=4.0,
        ops_runtime=5.0,
        shipping_runtime=6.0,
        start_by=start_by,
        is_labeled=is_labeled,
        are_photos_uploaded=are_photos_uploaded,
        start_by_urgency=start_by_urgency,
        ship_urgency=ship_urgency,
    )


def test_column_persistence_and_pinning(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "test_settings.ini"), QSettings.Format.IniFormat)
    picker = OpenAuditPicker(settings=settings)
    qtbot.addWidget(picker)

    header = picker.table_view.horizontalHeader()
    # Verify default widths were seeded
    assert header.sectionSize(Column.START_BY) == _DEFAULT_COLUMN_WIDTHS[Column.START_BY]
    assert header.sectionSize(Column.PART_NUMBER) == _DEFAULT_COLUMN_WIDTHS[Column.PART_NUMBER]

    # Verify Start-By is at visual index 0
    assert header.visualIndex(Column.START_BY) == 0

    # Try to move Start-By to visual index 3
    header.moveSection(0, 3)
    # _enforce_start_by_pinned should immediately move it back to 0
    assert header.visualIndex(Column.START_BY) == 0

    # Move another section (e.g. SHIP_DATE from 1 to 3)
    header.moveSection(1, 3)
    assert header.visualIndex(Column.SHIP_DATE) == 3
    # This should have persisted to settings
    assert settings.contains("audit_list/header_state")

    # Create a second picker with the same settings to verify restoration
    picker2 = OpenAuditPicker(settings=settings)
    qtbot.addWidget(picker2)
    header2 = picker2.table_view.horizontalHeader()
    assert header2.visualIndex(Column.START_BY) == 0
    assert header2.visualIndex(Column.SHIP_DATE) == 3

    # Reset columns to default
    picker2._reset_columns()
    assert header2.visualIndex(Column.SHIP_DATE) == 1
    assert not settings.contains("audit_list/header_state")


def test_centered_check_delegate_toggling(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    digest = create_digest(audit_id=42, is_labeled=False, are_photos_uploaded=False)
    picker.populate([digest])

    data_row = 1  # 0 is group header
    label_idx = picker.model.index(data_row, Column.LABEL)
    delegate = picker.table_view.itemDelegateForColumn(Column.LABEL)

    label_emitted = []
    picker.label_toggle_requested.connect(lambda a_id, val: label_emitted.append((a_id, val)))

    # Simulate Space key press in editorEvent
    key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
    delegate.editorEvent(key_event, picker.model, None, label_idx)

    assert label_emitted == [(42, True)]


def test_date_formatting_and_urgency_styling(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)

    start_date = date(2026, 7, 25)
    ship_date = date(2026, 7, 28)
    digest = create_digest(
        start_by=start_date,
        ship_date=ship_date,
        start_by_urgency=DateUrgency.OVERDUE,
        ship_urgency=DateUrgency.DUE_SOON,
    )
    picker.populate([digest])

    data_row = 1
    start_idx = picker.model.index(data_row, Column.START_BY)
    ship_idx = picker.model.index(data_row, Column.SHIP_DATE)
    part_idx = picker.model.index(data_row, Column.PART_NUMBER)

    # Check date display format (short month + day)
    assert picker.model.data(start_idx, Qt.ItemDataRole.DisplayRole) == "Jul 25"
    assert picker.model.data(ship_idx, Qt.ItemDataRole.DisplayRole) == "Jul 28"

    # Check FontRole (bold for date columns, None for other data columns)
    start_font = picker.model.data(start_idx, Qt.ItemDataRole.FontRole)
    assert start_font is not None and start_font.bold()
    assert picker.model.data(part_idx, Qt.ItemDataRole.FontRole) is None

    # Check ForegroundRole colors based on urgency
    assert picker.model.data(start_idx, Qt.ItemDataRole.ForegroundRole) == facelift.overdue_color()
    assert picker.model.data(ship_idx, Qt.ItemDataRole.ForegroundRole) == facelift.due_soon_color()
