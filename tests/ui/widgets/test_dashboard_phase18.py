import pytest
from unittest.mock import Mock, patch
from PyQt6.QtWidgets import QLabel
from cockpit.ui.widgets.dashboard import Dashboard
from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService
from cockpit.services.completion import CompletionService
from cockpit.services.audit_metadata import AuditMetadataService
from cockpit.ingestion.service import IngestionService
from cockpit.services.views import ActiveAuditView
from cockpit.persistence.types import AuditStatus

@pytest.fixture
def dashboard(qtbot):
    chk = Mock(spec=ChecklistService)
    splt = Mock(spec=AuditSplitService)
    comp = Mock(spec=CompletionService)
    meta = Mock(spec=AuditMetadataService)
    ing = Mock(spec=IngestionService)
    d = Dashboard(chk, splt, comp, meta, ing)
    qtbot.addWidget(d)
    return d

def test_dashboard_metadata_labels(dashboard):
    view = ActiveAuditView(
        audit_id=1,
        part_number="PN-123",
        work_order_ref="WO-1",
        split_suffix=None,
        quantity=10,
        split_reason=None,
        status=AuditStatus.IN_PROGRESS,
        tht_rows=[],
        notes_rows=[],
        general_notes=None,
        ship_date=None,
        traveler_metadata={"customer_name": "TestCorp"},
        has_pdf=False
    )
    dashboard._view = view
    dashboard._apply_view()
    
    labels = []
    for i in range(dashboard.metadata_layout.count()):
        widget = dashboard.metadata_layout.itemAt(i).widget()
        if isinstance(widget, QLabel):
            labels.append(widget.text())
            
    assert "Customer: TestCorp" in labels
    assert "S/O: —" in labels

def test_dashboard_back_flushes_and_exits(dashboard, qtbot):
    with patch.object(dashboard.audit_notes, "flush_pending") as mock_flush:
        with qtbot.waitSignal(dashboard.exit_requested):
            dashboard.header.back_requested.emit()
            
        mock_flush.assert_called_once()
