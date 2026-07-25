import pytest
from PyQt6.QtGui import QColor

def test_facelift_attention_color():
    from cockpit.ui.facelift import attention_color, set_active_preset, DARK, LIGHT
    set_active_preset(DARK)
    assert attention_color() == QColor("#FFA033")
    
    set_active_preset(LIGHT)
    assert attention_color() == QColor("#E65100")

def test_facelift_urgency_colors():
    from cockpit.ui.facelift import overdue_color, due_soon_color, set_active_preset, DARK, LIGHT
    set_active_preset(DARK)
    assert overdue_color() == QColor("#FF5C5C")
    assert due_soon_color() == QColor("#FFC542")
    
    set_active_preset(LIGHT)
    assert overdue_color() == QColor("#C62828")
    assert due_soon_color() == QColor("#B26A00")

def test_facelift_semantic_colors():
    from cockpit.ui.facelift import list_column_color, set_active_preset, DARK, LIGHT
    
    set_active_preset(DARK)
    assert list_column_color(2) == QColor("#59D7FF")
    assert list_column_color(5) == QColor("#7EE787")
    assert list_column_color(9) == QColor("#D2A8FF")
    
    set_active_preset(LIGHT)
    assert list_column_color(2) == QColor("#0277BD")
    assert list_column_color(5) == QColor("#2E7D32")
    assert list_column_color(9) == QColor("#6A1B9A")

def test_page_switcher_invariant(qtbot):
    from cockpit.ui.canvas.page_switcher import PageSwitcher
    switcher = PageSwitcher()
    qtbot.addWidget(switcher)
    switcher.set_page_count(2)
    
    # Click second tab
    switcher.buttons[1].click()
    
    assert switcher.buttons[1].property("indicator") is False
    assert switcher._current_index == 1
    
    # Try setting indicator on current active
    switcher.set_other_page_indicator(True)
    assert switcher.buttons[1].property("indicator") is False
    assert switcher.buttons[0].property("indicator") is True
