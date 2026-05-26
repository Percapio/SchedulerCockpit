import pytest
from cockpit.ui.widgets.audit_notes_view import AuditNotesView
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtCore import Qt
from unittest.mock import patch

def test_flush_pending_emits_once(qtbot):
    view = AuditNotesView()
    qtbot.addWidget(view)
    
    view.populate("old notes")
    view.text_edit.setPlainText("new notes")
    
    with qtbot.waitSignal(view.notes_commit_requested, timeout=500) as blocker:
        emitted = view.flush_pending()
        
    assert emitted is True
    assert blocker.args == ["new notes"]
    
    # Second flush should do nothing
    emitted2 = view.flush_pending()
    assert emitted2 is False

def test_focus_out_calls_flush_pending(qtbot):
    view = AuditNotesView()
    qtbot.addWidget(view)
    
    with patch.object(view, "flush_pending") as mock_flush:
        event = QFocusEvent(QFocusEvent.Type.FocusOut, Qt.FocusReason.MouseFocusReason)
        view._on_focus_out(event)
        
        mock_flush.assert_called_once()
