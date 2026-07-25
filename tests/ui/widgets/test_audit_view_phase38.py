import pytest
from unittest.mock import Mock
from PyQt6.QtWidgets import QLabel
from cockpit.ui.widgets.audit_view import AuditView
from cockpit.services.views import ActiveAuditView
from cockpit.persistence.types import AuditStatus


import pathlib
from cockpit.ui.theme import ThemeLoader

def test_audit_view_class_display(qtbot):
    chk = Mock()
    splt = Mock()
    comp = Mock()
    ing = Mock()
    lqs = Mock()
    rel = Mock()
    setb = Mock()
    rend = Mock()
    ui_dir = pathlib.Path(__file__).parent.parent.parent.parent / "cockpit" / "ui"
    theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")

    view_widget = AuditView(chk, splt, comp, ing, lqs, rel, setb, rend, theme=theme)
    qtbot.addWidget(view_widget)

    metadata = {
        "customer_name": "TestCorp",
        "sales_order_number": "SO-100",
        "lead_time_days": "10",
        "assembly_class": 3,
    }
    view_widget._on_metadata_changed(metadata)

    labels = [view_widget._metadata_layout.itemAt(i).widget().text() for i in range(view_widget._metadata_layout.count())]
    assert "Class 3" in labels

    # Test invalid assembly_class (should not crash or show Class X)
    view_widget._on_metadata_changed({"assembly_class": "invalid"})
    labels = [view_widget._metadata_layout.itemAt(i).widget().text() for i in range(view_widget._metadata_layout.count())]
    assert not any(text.startswith("Class") for text in labels)
