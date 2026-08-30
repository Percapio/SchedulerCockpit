"""Build Notes pane: renders the source .docx directly (Patch 08 §3.1, §5)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QPalette, QTextDocument, QTextTable
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cockpit.services.cell_copy import cell_mime_data
from cockpit.services.notes_renderer import RenderPalette, render_build_notes

logger = logging.getLogger(__name__)

SEARCH_DEBOUNCE_MS = 200

NOT_ATTACHED_MESSAGE = "No build notes attached"
NO_TABLES_MESSAGE = "No build-note tables in this document"


class BuildNotesPane(QWidget):
    """Read-only render of the audit's build-notes .docx.

    Rendering is lazy and driven by visibility alone: load() always returns the
    pane to Empty and stores the new path, so the pane never needs to know the
    pager's current page.
    """

    empty_space_clicked = pyqtSignal()

    def __init__(self, theme: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._docx_path: Path | None = None
        self._state = "Empty"
        self._document: QTextDocument | None = None
        self._palette = self._snapshot_palette()
        self._pending_query = ""

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._on_search_timeout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        self._editor = _NotesTextEdit(self)
        self._editor.setReadOnly(True)
        self._editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._editor.customContextMenuRequested.connect(self._on_context_menu)
        self._apply_document_ground()
        self._stack.addWidget(self._editor)

        self._message = QLabel("", self)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._stack.addWidget(self._message)

        self._show_message(NOT_ATTACHED_MESSAGE)

    # ------------------------------------------------------------ palette

    def _snapshot_palette(self) -> RenderPalette:
        return RenderPalette(
            page_background_rgb=self._theme.notes_page_background_rgb(),
            default_text_rgb=self._theme.notes_default_text_rgb(),
            placeholder_border_rgb=self._theme.notes_placeholder_border_rgb(),
            placeholder_text_rgb=self._theme.notes_placeholder_text_rgb(),
        )

    def _apply_document_ground(self) -> None:
        """The document renders on its own page ground, not the app's (§3.3)."""
        palette = self._editor.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(self._palette.page_background_rgb))
        palette.setColor(QPalette.ColorRole.Text, QColor(self._palette.default_text_rgb))
        self._editor.setPalette(palette)
        self._editor.viewport().setAutoFillBackground(True)

    def invalidate(self) -> None:
        """Discards the render so a theme change cannot leave it half-themed."""
        self._palette = self._snapshot_palette()
        self._apply_document_ground()
        self._discard_document()
        if self.isVisible():
            self._render()

    # ------------------------------------------------------------ lifecycle

    def load(self, view: Any) -> None:
        """Stores the audit's notes path and discards any rendered document."""
        self._discard_document()
        self._docx_path = getattr(view, "notes_docx_path", None) if view else None
        if self.isVisible():
            self._render()
        elif self._docx_path is None:
            self._show_message(NOT_ATTACHED_MESSAGE)

    def unload(self) -> None:
        self._docx_path = None
        self._discard_document()
        self._pending_query = ""
        self._search_timer.stop()
        self._show_message(NOT_ATTACHED_MESSAGE)

    def _discard_document(self) -> None:
        """Releases the document's C++ heap without waiting for the collector."""
        self._state = "Empty"
        self._editor.setExtraSelections([])
        stale = self._document
        self._document = None
        self._editor.setDocument(QTextDocument(self._editor))
        if stale is not None:
            stale.clear()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._state == "Empty":
            self._render()

    # ------------------------------------------------------------ rendering

    def _render(self) -> None:
        if self._state == "Rendered":
            return
        if self._docx_path is None:
            self._show_message(NOT_ATTACHED_MESSAGE)
            return

        outcome = render_build_notes(self._docx_path, self._palette)
        if not outcome.is_ok():
            self._show_message(self._failure_message(outcome.err))
            return

        rendered = outcome.ok
        document = rendered.document
        document.setDefaultFont(self._editor.font())
        self._document = document
        self._editor.setDocument(document)
        self._apply_wrap(rendered.natural_width)
        self._state = "Rendered"
        self._stack.setCurrentWidget(self._editor)

        if rendered.anomalies:
            logger.info(
                "Build notes rendered with %d anomalies: %s",
                len(rendered.anomalies),
                {anomaly.reason for anomaly in rendered.anomalies},
            )
        if self._pending_query:
            self.highlight_matches(self._pending_query)

    def _apply_wrap(self, natural_width: int) -> None:
        """The document renders at its true width and scrolls; it does not squeeze.

        FixedPixelWidth rather than a one-shot setTextWidth: QTextEdit relays
        the document out against its viewport on every resize and on
        setDocument, which would silently undo a text width set by hand.
        A document that declares no widths has no true width, so it wraps.
        """
        if natural_width > 0:
            self._editor.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
            self._editor.setLineWrapColumnOrWidth(natural_width)
        else:
            self._editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)

    def _failure_message(self, failure: Any) -> str:
        if failure.reason == "DocumentMissing":
            return (
                f"Build notes file is missing:\n{failure.message}\n\n"
                "Re-ingest this audit to restore it."
            )
        if failure.reason == "DocumentUnreadable":
            return f"Build notes could not be read:\n{failure.message}"
        return NO_TABLES_MESSAGE

    def _show_message(self, text: str) -> None:
        self._message.setText(text)
        self._stack.setCurrentWidget(self._message)

    # ------------------------------------------------------------ search

    def queue_highlight(self, query: str) -> None:
        """Debounced entry point; a full find walk per keystroke is not free."""
        self._pending_query = query
        self._search_timer.start()

    def _on_search_timeout(self) -> None:
        self.highlight_matches(self._pending_query)

    def highlight_matches(self, query: str) -> int:
        """Highlights every occurrence of query in the rendered document.

        post: returns the number of matches; clears all extra selections and
              returns 0 for an empty query or when no document has been rendered
        """
        self._pending_query = query
        if self._state != "Rendered" or self._document is None or not query:
            self._editor.setExtraSelections([])
            return 0

        colour = QColor(self._theme.notes_search_highlight_rgb())
        selections: list[QTextEdit.ExtraSelection] = []
        cursor = self._document.find(query)
        while not cursor.isNull():
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format.setBackground(colour)
            selections.append(selection)
            cursor = self._document.find(query, cursor)

        self._editor.setExtraSelections(selections)
        return len(selections)

    # ------------------------------------------------------------ interaction

    def _table_at(self, position) -> QTextTable | None:
        """The table under a viewport point, or None for empty space."""
        return self._editor.cursorForPosition(position).currentTable()

    def _on_viewport_press(self, position) -> bool:
        """Returns True when the press landed outside every table."""
        if self._table_at(position) is None:
            self.empty_space_clicked.emit()
            return True
        return False

    def _on_context_menu(self, position) -> None:
        table = self._table_at(position)
        if table is None or self._document is None:
            return
        cell = table.cellAt(self._editor.cursorForPosition(position))
        if not cell.isValid():
            return

        menu = QMenu(self)
        copy_action = QAction("Copy cell", menu)
        copy_action.triggered.connect(lambda: self._copy_cell(cell))
        menu.addAction(copy_action)
        menu.exec(self._editor.viewport().mapToGlobal(position))

    def _copy_cell(self, cell) -> None:
        if self._document is None:
            return
        QApplication.clipboard().setMimeData(cell_mime_data(cell, self._document))


class _NotesTextEdit(QTextEdit):
    """QTextEdit that reports presses on empty space to its owning pane."""

    def __init__(self, pane: BuildNotesPane) -> None:
        super().__init__(pane)
        self._pane = pane

    def mousePressEvent(self, event) -> None:
        if self._pane._on_viewport_press(event.pos()):
            return
        super().mousePressEvent(event)
