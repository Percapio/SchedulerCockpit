import pytest
from datetime import datetime, timezone
from cockpit.ui.widgets.open_audit_picker import OpenAuditPicker
from cockpit.services.views import OpenAuditDigest
from cockpit.persistence.types import AuditStatus

def test_picker_table_grouping(qtbot):
    digest = OpenAuditDigest(
        audit_id=1,
        part_number="PN-123",
        work_order_ref="WO-456",
        split_suffix="",
        quantity=10,
        status=AuditStatus.NOT_CLEAR,
        updated_at=datetime.now(timezone.utc),
        date_ingested=datetime.now(timezone.utc),
        ship_date=None,
        lead_time_days=None,
        repeat="NEW",
        classification="Non-ITAR",
        assembly_class=2,
        process=None,
        feeder_setuptime=None,
        smt_runtime=None,
        tht_runtime=None,
        start_by=None
    )
    
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([digest])
    
    # Header row for NOT_CLEAR should span all columns
    assert picker.table_view.rowSpan(0, 0) == 1
    assert picker.table_view.columnSpan(0, 0) == picker.model.columnCount()
