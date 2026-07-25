import pytest
from unittest.mock import Mock, patch
from PyQt6.QtWidgets import QLabel
from cockpit.ui.widgets.dashboard import Dashboard
from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService
from cockpit.services.completion import CompletionService
from cockpit.ingestion.service import IngestionService
from cockpit.services.views import ActiveAuditView
from cockpit.persistence.types import AuditStatus

@pytest.fixture
def dashboard(qtbot):
    chk = Mock(spec=ChecklistService)
    splt = Mock(spec=AuditSplitService)
    comp = Mock(spec=CompletionService)
    ing = Mock(spec=IngestionService)
    theme = Mock()
    rel = Mock()
    setb = Mock()
    d = Dashboard(chk, splt, comp, ing, rel, setb, theme)
    qtbot.addWidget(d)
    return d

def test_dashboard_metadata_emitted(dashboard, qtbot):
    view = ActiveAuditView(
        audit_id=1,
        part_number="PN-123",
        work_order_ref="WO-1",
        split_suffix=None,
        quantity=10,
        split_reason=None,
        status=AuditStatus.NOT_CLEAR,
        tht_rows=[],
        notes_rows=[],
        traveler_metadata={"customer_name": "TestCorp"},
        has_pdf=False,
        tht_placement_count=0
    )
    dashboard._view = view
    
    with qtbot.waitSignal(dashboard.metadata_changed, timeout=1000) as blocker:
        dashboard._apply_view()
        
    assert blocker.args[0] == {"customer_name": "TestCorp"}

def test_dashboard_back_flushes_and_exits(dashboard, qtbot):
    with qtbot.waitSignal(dashboard.exit_requested):
        dashboard.header.back_requested.emit()

def test_dashboard_secondary_drawing_action(dashboard):
    view = ActiveAuditView(
        audit_id=1,
        part_number="PN-123",
        work_order_ref="WO-1",
        split_suffix=None,
        quantity=10,
        split_reason=None,
        status=AuditStatus.NOT_CLEAR,
        tht_rows=[],
        notes_rows=[],
        traveler_metadata={},
        has_pdf=True,
        has_secondary_pdf=False,
        tht_placement_count=0
    )
    dashboard._view = view
    dashboard._rebuild_actions_menu()
    
    actions = [a.text() for a in dashboard.actions_menu.actions()]
    assert "Add Secondary Drawing" in actions

    import dataclasses
    dashboard._view = dataclasses.replace(view, has_secondary_pdf=True)
    dashboard._rebuild_actions_menu()
    actions = [a.text() for a in dashboard.actions_menu.actions()]
    assert "Replace Secondary Drawing" in actions
