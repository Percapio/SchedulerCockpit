import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QTextDocument
from cockpit.ui.widgets.build_notes_pane import BuildNotesPane

class MockTheme:
    def theme_value(self, key, default):
        return default
    def pane_gap_px(self): return 0
    def notes_column_min_width_px(self): return 0
    def notes_column_max_content_width_px(self): return 1000

class MockAudit:
    def notes_file_path(self):
        return None

def test_build_notes_pane_empty(qtbot):
    pane = BuildNotesPane(MockTheme())
    qtbot.addWidget(pane)
    assert pane._state == 'Empty'

def test_build_notes_pane_load(qtbot):
    pane = BuildNotesPane(MockTheme())
    qtbot.addWidget(pane)
    pane.load(MockAudit())
    pane.show() # should trigger render
    qtbot.waitUntil(lambda: pane._state == 'Empty' or pane._error_label.isVisible())
