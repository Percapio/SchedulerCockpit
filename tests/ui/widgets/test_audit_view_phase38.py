import pytest
from unittest.mock import Mock
from PyQt6.QtWidgets import QLabel
from cockpit.ui.widgets.audit_identity_bar import AuditIdentityBar
from cockpit.services.views import AuditIdentityBanner

def test_audit_identity_bar_updates(qtbot):
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    
    banner = AuditIdentityBanner(
        sales_order="SO-100",
        part_number="PN-123",
        quantity="10",
        lead_time_days="10",
        assembly_class="Class 3",
        process="",
        customer="TestCorp",
        repeat_marker="",
        status="NOT_CLEAR",
        is_itar=False
    )
    bar.set_identity(banner)
    
    # Just assert no crash, child count stable
    child_count_before = len(bar.findChildren(QLabel))
    bar.set_identity(banner)
    child_count_after = len(bar.findChildren(QLabel))
    
    assert child_count_before == child_count_after
