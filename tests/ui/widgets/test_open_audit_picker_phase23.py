"""Tests for OpenAuditPicker (Phase 23)."""

import pytest
from datetime import datetime, timezone, timedelta
from PyQt6.QtWidgets import QPushButton, QLabel
from PyQt6.QtCore import Qt

from cockpit.services.views import OpenAuditDigest
from cockpit.ui.widgets.open_audit_picker import OpenAuditPicker, format_updated_stamp, PickerRow

def test_format_updated_stamp_utc_to_pst():
    # 2026-06-01 02:05:00 UTC is 2026-05-31 18:05:00 PST (6:05 PM)
    moment = datetime(2026, 6, 1, 2, 5, 0, tzinfo=timezone.utc)
    stamp = format_updated_stamp(moment)
    assert stamp == "2026-05-31, 6:05 PM"

def test_format_updated_stamp_midnight():
    # 2026-05-31 08:30:00 UTC is 2026-05-31 00:30:00 PST (12:30 AM)
    moment = datetime(2026, 5, 31, 8, 30, 0, tzinfo=timezone.utc)
    stamp = format_updated_stamp(moment)
    assert stamp == "2026-05-31, 12:30 AM"

def test_format_updated_stamp_noon():
    # 2026-05-31 20:00:00 UTC is 2026-05-31 12:00:00 PST (12:00 PM)
    moment = datetime(2026, 5, 31, 20, 0, 0, tzinfo=timezone.utc)
    stamp = format_updated_stamp(moment)
    assert stamp == "2026-05-31, 12:00 PM"

def test_format_updated_stamp_requires_tzinfo():
    with pytest.raises(ValueError):
        format_updated_stamp(datetime(2026, 5, 31, 12, 0, 0))

def test_OpenAuditPicker_font_scale_buttons(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    
    emitted = []
    picker.font_scale_change_requested.connect(emitted.append)
    
    btns = picker.findChildren(QPushButton)
    minus = next(b for b in btns if b.text() == "A-")
    plus = next(b for b in btns if b.text() == "A+")
    
    minus.click()
    assert emitted == [-1]
    
    emitted.clear()
    plus.click()
    assert emitted == [1]

def test_OpenAuditPicker_has_only_one_title(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    titles = [l for l in picker.findChildren(QLabel) if l.text() == "Select an Audit"]
    assert len(titles) == 1

def test_OpenAuditPicker_populate_and_click(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    
    digest = OpenAuditDigest(
        audit_id=42,
        part_number="PN-123",
        work_order_ref="WO-999",
        split_suffix="-B",
        quantity=500,
        status="Not Clear",
        updated_at=datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    )
    picker.populate([digest])
    
    assert picker.list_widget.count() == 1
    item = picker.list_widget.item(0)
    row = picker.list_widget.itemWidget(item)
    assert isinstance(row, PickerRow)
    
    emitted = []
    picker.audit_selected.connect(emitted.append)
    
    # Simulate row click
    row.selected.emit(42)
    assert emitted == [42]
    assert picker.list_widget.currentItem() == item
