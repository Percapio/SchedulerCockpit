import pytest
from cockpit.ui.widgets.identity_header import IdentityHeader
from cockpit.services.views import ActiveAuditView
from cockpit.persistence.types import AuditStatus

def test_identity_header_title_stripping(qtbot):
    header = IdentityHeader()
    qtbot.addWidget(header)
    
    view = ActiveAuditView(
        audit_id=1,
        part_number="PN-123",
        work_order_ref="WO-1",
        split_suffix="-B",
        quantity=10,
        split_reason=None,
        status=AuditStatus.IN_PROGRESS,
        tht_rows=[],
        notes_rows=[],
        ship_date=None,
        traveler_metadata={"sales_order_number": "SO-999"},
        has_pdf=False
    )
    
    header.set_audit(view)
    assert header.title_lbl.text() == "PN-123-B"

def test_ship_date_field_label(qtbot):
    from cockpit.ui.widgets.ship_date_field import ShipDateField
    fld = ShipDateField()
    qtbot.addWidget(fld)
    assert fld._label.text() == "Ship Date"
