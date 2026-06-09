import pytest
from unittest.mock import MagicMock, patch
from datetime import date

from cockpit.services.release import ReleaseService, ReleaseFormData
from cockpit.services.views import ActiveAuditView
from cockpit.persistence.types import AuditStatus
from cockpit.persistence.errors import AuditNotFound

def test_print_release_form_empty_optional():
    data = ReleaseFormData(
        assembly_number="123",
        quantity=10,
        lead_time_days=5,
        repeat="New",
        assembly_modifier=None,
        itar_display="",
        process_clean="",
        class_display="",
        process="",
        ship_date="",
        turn_note="",
        floor_notes="",
        shortages_notes="",
        pcb_clear="",
        setup_first_side="",
        program_in_kit=False,
        folder_in_kit=False
    )
    
    printer = MagicMock()
    with patch("PyQt6.QtGui.QTextDocument") as mock_doc_cls:
        mock_doc = MagicMock()
        mock_doc_cls.return_value = mock_doc
        
        service = ReleaseService(MagicMock())
        service.print_release_form(data, printer)
        
        html_content = mock_doc.setHtml.call_args[0][0]
        
        # Assert optional sections and their separators are missing
        assert "HOT JOB:" not in html_content
        assert "PCB Clear Date:" not in html_content
        assert "Shortages Notes:" not in html_content
        
        # Assert mandatory ship date section is still present
        assert "<p><b>Ship Date:</b> </p>" in html_content

def test_print_release_form_populated_optional():
    data = ReleaseFormData(
        assembly_number="123",
        quantity=10,
        lead_time_days=5,
        repeat="New",
        assembly_modifier=None,
        itar_display="",
        process_clean="",
        class_display="",
        process="",
        ship_date="2026-07-01",
        turn_note="ASAP",
        floor_notes="",
        shortages_notes="Missing caps",
        pcb_clear="Not Clear",
        setup_first_side="",
        program_in_kit=False,
        folder_in_kit=False
    )
    
    printer = MagicMock()
    with patch("PyQt6.QtGui.QTextDocument") as mock_doc_cls:
        mock_doc = MagicMock()
        mock_doc_cls.return_value = mock_doc
        
        service = ReleaseService(MagicMock())
        service.print_release_form(data, printer)
        
        html_content = mock_doc.setHtml.call_args[0][0]
        
        # Assert optional sections are present
        assert "<p><b>HOT JOB:</b> ASAP</p>" in html_content
        assert "<p><b>PCB Clear Date:</b> Not Clear</p>" in html_content
        assert "<p><b>Shortages Notes:</b> Missing caps</p>" in html_content
        
        # Assert mandatory ship date section is present
        assert "<p><b>Ship Date:</b> 2026-07-01</p>" in html_content

def test_build_defaults_seeding():
    service = ReleaseService(MagicMock())
    view_no_date = ActiveAuditView(
        audit_id=1,
        part_number="PN",
        work_order_ref="WO",
        split_suffix="",
        quantity=10,
        status=AuditStatus.THT,
        split_reason=None,
        traveler_metadata={},
        has_pdf=False,
        ship_date=None
    )
    res_no_date = service.build_defaults(view_no_date)
    assert res_no_date.ship_date == ""
    
    view_with_date = ActiveAuditView(
        audit_id=1,
        part_number="PN",
        work_order_ref="WO",
        split_suffix="",
        quantity=10,
        status=AuditStatus.THT,
        split_reason=None,
        traveler_metadata={},
        has_pdf=False,
        ship_date="2026-07-01"
    )
    res_with_date = service.build_defaults(view_with_date)
    assert res_with_date.ship_date == "2026-07-01"

def test_persist_release():
    mock_repo = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_repo.conn = mock_conn
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.rowcount = 1
    
    service = ReleaseService(mock_repo)
    
    # Test blank (None) date
    service.persist_release(1, AuditStatus.THT, None)
    args = mock_cursor.execute.call_args[0]
    assert args[0] == "UPDATE active_audits SET status = ?, ship_date = ?, updated_at = ? WHERE id = ?"
    assert args[1][1] is None  # ship_str is None
    
    # Test populated ISO date
    service.persist_release(1, AuditStatus.THT, date(2026, 7, 1))
    args2 = mock_cursor.execute.call_args[0]
    assert args2[1][1] == "2026-07-01"
    
    # Test AuditNotFound
    mock_cursor.rowcount = 0
    with pytest.raises(AuditNotFound):
        service.persist_release(999, AuditStatus.THT, None)
