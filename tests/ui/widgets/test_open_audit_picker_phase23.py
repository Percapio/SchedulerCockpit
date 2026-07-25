"""Tests for OpenAuditPicker (Phase 23/28)."""

import pytest
from datetime import datetime, timezone, timedelta
from PyQt6.QtWidgets import QPushButton, QLabel
from PyQt6.QtCore import Qt, QModelIndex

from cockpit.services.views import OpenAuditDigest
from cockpit.persistence.types import AuditStatus
from cockpit.ui.widgets.open_audit_picker import OpenAuditPicker, RowKind

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
        status=AuditStatus.NOT_CLEAR,
        updated_at=datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc),
        date_ingested=datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc),
        ship_date=None,
        lead_time_days=None,
        repeat="NEW",
        classification="Non-ITAR",
        assembly_class=2,
        process=None,
        feeder_setuptime=None,
        smt_runtime=None,
        tht_runtime=None,
        aoi_runtime=None,
        ops_runtime=None,
        shipping_runtime=None,
        start_by=None
    )
    picker.populate([digest])
    
    # We should have a group header and a data row
    assert picker.model.rowCount() == 2
    
    # Find the data row index
    data_idx = picker.model.index(1, 0)
    assert picker.model.data(data_idx, Qt.ItemDataRole.UserRole)["kind"] == RowKind.DATA
    
    emitted = []
    picker.audit_selected.connect(emitted.append)
    
    # Simulate table click
    picker.table_view.clicked.emit(data_idx)
    assert emitted == [42]
