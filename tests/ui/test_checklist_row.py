import pytest
from unittest.mock import MagicMock
from PyQt6.QtWidgets import QApplication

from cockpit.services.views import ChecklistRowView, ChecklistRowKey, ChecklistRowKind
from cockpit.ui.widgets.checklist_row import ChecklistRow


@pytest.fixture
def dummy_view():
    return ChecklistRowView(
        key=ChecklistRowKey(kind=ChecklistRowKind.THT, item_id=1),
        is_verified=False,
        primary_label="R123",
        secondary_label="10k Resistor"
    )


def test_ChecklistRow_SetSelectedTrue_SetsPropertyAndRepolishesStyle(qtbot, monkeypatch, dummy_view):
    row = ChecklistRow(dummy_view)
    qtbot.addWidget(row)
    
    # Mock style methods to track polish/unpolish calls
    style_unpolish_mock = MagicMock()
    style_polish_mock = MagicMock()
    monkeypatch.setattr(row.style(), "unpolish", style_unpolish_mock)
    monkeypatch.setattr(row.style(), "polish", style_polish_mock)
    
    row.set_selected(True)
    
    assert row.property("selected") is True
    style_unpolish_mock.assert_any_call(row)
    style_polish_mock.assert_any_call(row)


def test_ChecklistRow_SetSelectedFalse_ClearsPropertyAndRepolishesStyle(qtbot, monkeypatch, dummy_view):
    row = ChecklistRow(dummy_view)
    qtbot.addWidget(row)
    
    # Mock style methods to track polish/unpolish calls
    style_unpolish_mock = MagicMock()
    style_polish_mock = MagicMock()
    monkeypatch.setattr(row.style(), "unpolish", style_unpolish_mock)
    monkeypatch.setattr(row.style(), "polish", style_polish_mock)
    
    # initially true to ensure transition
    row.setProperty("selected", True)
    
    row.set_selected(False)
    
    assert row.property("selected") is False
    style_unpolish_mock.assert_any_call(row)
    style_polish_mock.assert_any_call(row)
