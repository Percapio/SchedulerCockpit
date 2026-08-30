from typing import Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QLabel, QApplication
)
from PyQt6.QtGui import (
    QTextDocument, QAction, QTextCursor, QTextDocumentFragment, QColor, QPalette
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QEvent

from cockpit.services.notes_renderer import render_build_notes, RenderPalette, RenderedNotes, NotesRenderFailure

class BuildNotesPane(QWidget):
    """Displays build notes rendered from the original .docx file."""
    
    empty_space_clicked = pyqtSignal()
    
    def __init__(self, theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._audit = None
        self._state = "Empty"
        self._document = None
        self._palette = RenderPalette(
            page_background_rgb=theme.theme_value("notes.page_background_rgb", "#FFFFFF"),
            default_text_rgb=theme.theme_value("notes.default_text_rgb", "#000000"),
            placeholder_border_rgb=theme.theme_value("notes.placeholder_border_rgb", "#FF0000"),
            placeholder_text_rgb=theme.theme_value("notes.placeholder_text_rgb", "#FF0000"),
        )
        # We need a debounce timer for search
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._perform_search)
        self._last_query = ""
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        self._editor = QTextEdit(self)
        self._editor.setReadOnly(True)
        self._editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._editor.customContextMenuRequested.connect(self._show_context_menu)
        self._editor.viewport().installEventFilter(self)
        
        # Apply palette to widget
        pal = self._editor.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(self._palette.page_background_rgb))
        self._editor.setPalette(pal)
        
        main_layout.addWidget(self._editor)
        
        self._error_label = QLabel(self)
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.hide()
        main_layout.addWidget(self._error_label)

    def load(self, audit) -> None:
        """Store path and discard any document."""
        self.unload()
        self._audit = audit
        if self.isVisible():
            self._render()
            
    def _render(self) -> None:
        if self._state == "Rendered" or not self._audit:
            return
            
        docx_path = self._audit.notes_file_path()
        if not docx_path:
            self._show_error("DocumentMissing", "No build notes file found.")
            return
            
        result = render_build_notes(docx_path, self._palette)
        if not result.is_ok():
            self._show_error(result.err.reason, result.err.message)
            return
            
        self._document = result.ok.document
        self._editor.setDocument(self._document)
        self._error_label.hide()
        self._editor.show()
        self._state = "Rendered"
        
        # apply previous search if any
        if self._last_query:
            self.highlight_matches(self._last_query)
            
    def _show_error(self, reason: str, message: str):
        self._error_label.setText(f"Error: {reason}\n{message}")
        self._editor.hide()
        self._error_label.show()
        
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._state == "Empty" and self._audit:
            self._render()

    def unload(self) -> None:
        """Discard to Empty."""
        self._state = "Empty"
        self._editor.setDocument(QTextDocument())
        if self._document:
            self._document.clear()
        self._document = None
        
    def highlight_matches(self, query: str) -> int:
        self._last_query = query
        self._search_timer.start()
        return 0 # The caller might expect count immediately, but we debounced.
        
    def _perform_search(self):
        query = self._last_query
        if self._state != "Rendered" or not self._document:
            return 0
            
        # clear existing selections
        selections = []
        if not query:
            self._editor.setExtraSelections(selections)
            return 0
            
        cursor = self._document.find(query)
        # highlight color
        highlight_hex = self._theme.theme_value("notes.search_highlight_rgb", "#FFFF00")
        highlight_color = QColor(highlight_hex)
        
        count = 0
        while not cursor.isNull():
            from PyQt6.QtWidgets import QTextEdit
            fmt = cursor.charFormat()
            fmt.setBackground(highlight_color)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = fmt
            selections.append(sel)
            
            cursor = self._document.find(query, cursor)
            count += 1
            if count > 1000: # safety guard
                break
                
        self._editor.setExtraSelections(selections)
        return count

    def eventFilter(self, obj, event) -> bool:
        if obj == self._editor.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            pos = event.pos()
            cursor = self._editor.cursorForPosition(pos)
            table = cursor.currentTable()
            if not table:
                self.empty_space_clicked.emit()
        return super().eventFilter(obj, event)
        
    def _show_context_menu(self, pos):
        cursor = self._editor.cursorForPosition(pos)
        table = cursor.currentTable()
        if not table:
            return
            
        cell = table.cellAt(cursor)
        
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        copy_action = QAction("Copy cell", self)
        copy_action.triggered.connect(lambda: self._copy_cell(cell))
        menu.addAction(copy_action)
        menu.exec(self._editor.mapToGlobal(pos))
        
    def _copy_cell(self, cell):
        if not self._document: return
        cursor = cell.firstCursorPosition()
        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.MoveMode.KeepAnchor)
        frag = cursor.selection()
        html = frag.toHtml()
        
        # Rewrite src to data: URI
        import base64
        import re
        
        def _replace_src(m):
            src = m.group(1)
            if src.startswith('notes-img:'):
                name = src
                res = self._document.resource(QTextDocument.ResourceType.ImageResource, QUrl(name))
                if res and isinstance(res, QImage):
                    # convert QImage to PNG base64
                    ba = QByteArray()
                    buf = QBuffer(ba)
                    buf.open(QBuffer.OpenModeFlag.WriteOnly)
                    res.save(buf, "PNG")
                    b64 = base64.b64encode(ba.data()).decode('utf-8')
                    return f'src="data:image/png;base64,{b64}"'
            return m.group(0)
            
        html = re.sub(r'src="([^"]+)"', _replace_src, html)
        text = frag.toPlainText()
        text = text.replace('\ufffc', '') # remove image placeholder char
        
        from PyQt6.QtCore import QMimeData
        md = QMimeData()
        md.setHtml(html)
        md.setText(text)
        QApplication.clipboard().setMimeData(md)
