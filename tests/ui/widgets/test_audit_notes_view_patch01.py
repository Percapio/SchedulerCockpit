import pytest
from PyQt6.QtCore import Qt
from cockpit.ui.widgets.audit_notes_view import AuditNotesView

def test_audit_notes_view_clear_emits_none(qtbot):
    view = AuditNotesView()
    qtbot.addWidget(view)
    
    # Setup initial state
    view.populate("Initial text")
    assert view._last_saved_text == "Initial text"
    
    # Hook signal
    emitted_payloads = []
    view.notes_commit_requested.connect(emitted_payloads.append)
    
    # User clears text and saves
    view.text_edit.setPlainText("")
    view.flush_pending()
    
    # Assert
    assert len(emitted_payloads) == 1
    assert emitted_payloads[0] is None
