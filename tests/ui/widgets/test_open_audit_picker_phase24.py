import pytest
from datetime import datetime, timezone
from cockpit.ui.widgets.open_audit_picker import PickerRow
from cockpit.services.views import OpenAuditDigest
from cockpit.persistence.types import AuditStatus

def test_picker_row_spacing(qtbot):
    digest = OpenAuditDigest(
        audit_id=1,
        part_number="PN-123",
        work_order_ref="WO-456",
        split_suffix="",
        quantity=10,
        status=AuditStatus.NOT_CLEAR,
        updated_at=datetime.now(timezone.utc)
    )
    
    row = PickerRow(digest)
    qtbot.addWidget(row)
    
    assert row.layout().spacing() == 12
