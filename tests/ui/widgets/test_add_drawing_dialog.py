import pytest
from pathlib import Path
from PyQt6.QtCore import Qt, QUrl, QMimeData
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QDialog

from cockpit.ui.widgets.add_drawing_dialog import AddDrawingDialog
from cockpit.ingestion.errors import IngestionError
from cockpit.persistence.errors import PersistenceError

class MockIngestionService:
    def __init__(self):
        self.called_with = None
        self.raise_ingestion_error = False
        self.raise_persistence_error = False
        
    def add_pdf_to_audit(self, audit_id, pdf_path):
        if self.raise_ingestion_error:
            raise IngestionError(pdf_path, "Bad PDF", {})
        if self.raise_persistence_error:
            raise PersistenceError("DB locked")
        self.called_with = (audit_id, pdf_path)


class MockMimeData(QMimeData):
    def __init__(self, urls):
        super().__init__()
        self._urls = urls
        
    def hasUrls(self):
        return bool(self._urls)
        
    def urls(self):
        return self._urls


class MockDragEnterEvent(QDragEnterEvent):
    def __init__(self, mime_data):
        from PyQt6.QtCore import QPoint
        # We only mock the necessary parts for the test
        super().__init__(QPoint(0, 0), Qt.DropAction.CopyAction, mime_data, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        self._accepted = False
        
    def acceptProposedAction(self):
        self._accepted = True
        
    def isAccepted(self):
        return self._accepted


class MockDropEvent(QDropEvent):
    def __init__(self, mime_data):
        from PyQt6.QtCore import QPointF
        super().__init__(QPointF(0, 0), Qt.DropAction.CopyAction, mime_data, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)


def test_drag_enter_accepts_single_pdf(qtbot):
    service = MockIngestionService()
    dialog = AddDrawingDialog(service, 1)
    qtbot.addWidget(dialog)
    
    # Accept single PDF
    mime = MockMimeData([QUrl.fromLocalFile("test.pdf")])
    event = MockDragEnterEvent(mime)
    dialog.dragEnterEvent(event)
    assert event.isAccepted()
    
    # Reject multiple files
    mime = MockMimeData([QUrl.fromLocalFile("test.pdf"), QUrl.fromLocalFile("test2.pdf")])
    event = MockDragEnterEvent(mime)
    dialog.dragEnterEvent(event)
    assert not event.isAccepted()
    
    # Reject non-PDF
    mime = MockMimeData([QUrl.fromLocalFile("test.txt")])
    event = MockDragEnterEvent(mime)
    dialog.dragEnterEvent(event)
    assert not event.isAccepted()
    
    # Reject non-local
    mime = MockMimeData([QUrl("http://example.com/test.pdf")])
    event = MockDragEnterEvent(mime)
    dialog.dragEnterEvent(event)
    assert not event.isAccepted()


def test_drop_event_success(qtbot):
    service = MockIngestionService()
    dialog = AddDrawingDialog(service, 42)
    qtbot.addWidget(dialog)
    
    mime = MockMimeData([QUrl.fromLocalFile("test.pdf")])
    event = MockDropEvent(mime)
    
    # Will call accept() on success
    with qtbot.waitSignal(dialog.accepted):
        dialog.dropEvent(event)
        
    assert service.called_with == (42, Path("test.pdf"))


def test_drop_event_ingestion_error(qtbot):
    service = MockIngestionService()
    service.raise_ingestion_error = True
    dialog = AddDrawingDialog(service, 42)
    qtbot.addWidget(dialog)
    
    mime = MockMimeData([QUrl.fromLocalFile("test.pdf")])
    event = MockDropEvent(mime)
    
    dialog.dropEvent(event)
    
    assert dialog.result() == QDialog.DialogCode.Rejected # hasn't closed accepted
    assert not dialog._error_label.isHidden()
    assert "Bad PDF" in dialog._error_label.text()


def test_drop_event_persistence_error(qtbot):
    service = MockIngestionService()
    service.raise_persistence_error = True
    dialog = AddDrawingDialog(service, 42)
    qtbot.addWidget(dialog)
    
    mime = MockMimeData([QUrl.fromLocalFile("test.pdf")])
    event = MockDropEvent(mime)
    
    dialog.dropEvent(event)
    
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog._error_label.isHidden()
    assert "DB locked" in dialog._error_label.text()


def test_cancel_button(qtbot):
    service = MockIngestionService()
    dialog = AddDrawingDialog(service, 42)
    qtbot.addWidget(dialog)
    
    with qtbot.waitSignal(dialog.rejected):
        qtbot.mouseClick(dialog._cancel_btn, Qt.MouseButton.LeftButton)
