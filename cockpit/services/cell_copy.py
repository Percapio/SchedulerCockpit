"""Clipboard payload for one rendered build-note table cell (Patch 08 §4).

Qt's own copy emits document-internal resource URLs and U+FFFC for images,
neither of which survives a paste into another application, so the payload is
constructed here.
"""

from __future__ import annotations

import base64

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QUrl
from PyQt6.QtGui import (
    QImage,
    QTextBlock,
    QTextCursor,
    QTextDocument,
    QTextDocumentFragment,
    QTextTable,
    QTextTableCell,
)

OBJECT_REPLACEMENT_CHARACTER = "￼"


def _image_names_in(fragment: QTextDocumentFragment) -> set[str]:
    """Resource names of every image the fragment carries.

    The names are the ones the renderer chose, so the src rewrite below is a
    lookup over a known set rather than a regex against Qt's HTML writer.
    """
    scratch = QTextDocument()
    QTextCursor(scratch).insertFragment(fragment)

    names: set[str] = set()

    def scan_block(block: QTextBlock) -> None:
        iterator = block.begin()
        while not iterator.atEnd():
            fragment_piece = iterator.fragment()
            if fragment_piece.isValid():
                char_format = fragment_piece.charFormat()
                if char_format.isImageFormat():
                    name = char_format.toImageFormat().name()
                    if name:
                        names.add(name)
            iterator += 1

    block = scratch.begin()
    while block.isValid():
        scan_block(block)
        block = block.next()

    for table in _tables_of(scratch):
        for row in range(table.rows()):
            for column in range(table.columns()):
                cell = table.cellAt(row, column)
                cell_block = cell.begin()
                while not cell_block.atEnd():
                    scan_block(cell_block.currentBlock())
                    cell_block += 1
    return names


def _tables_of(document: QTextDocument) -> list[QTextTable]:
    tables: list[QTextTable] = []
    cursor = QTextCursor(document)
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    seen: set[int] = set()
    while True:
        table = cursor.currentTable()
        if table is not None and id(table) not in seen:
            seen.add(id(table))
            tables.append(table)
        if not cursor.movePosition(QTextCursor.MoveOperation.NextBlock):
            break
    return tables


def _data_uri(document: QTextDocument, name: str) -> str | None:
    """PNG data: URI for a registered image resource, or None when absent."""
    resource = document.resource(QTextDocument.ResourceType.ImageResource, QUrl(name))
    image = QImage(resource) if not isinstance(resource, QImage) else resource
    if image.isNull():
        return None

    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "PNG"):
            return None
    finally:
        buffer.close()
    encoded = base64.b64encode(bytes(payload)).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _inline_images(html: str, document: QTextDocument, names: set[str]) -> str:
    for name in names:
        data_uri = _data_uri(document, name)
        if data_uri is None:
            continue
        for quote in ('"', "'"):
            html = html.replace(
                f"src={quote}{name}{quote}", f'src="{data_uri}"'
            )
    return html


def cell_mime_data(cell: QTextTableCell, document: QTextDocument) -> QMimeData:
    """Clipboard contents for one table cell.

    pre:  cell belongs to a table in the rendered document
    post: text/plain carries the cell's text with image placeholders removed;
          text/html carries the same content with every image inlined as a
          base64 data: URI, so a paste into Word or Outlook keeps the images
    """
    cursor = cell.firstCursorPosition()
    cursor.setPosition(
        cell.lastCursorPosition().position(), QTextCursor.MoveMode.KeepAnchor
    )
    fragment = cursor.selection()

    html = _inline_images(
        fragment.toHtml(), document, _image_names_in(fragment)
    )
    text = fragment.toPlainText().replace(OBJECT_REPLACEMENT_CHARACTER, "")

    payload = QMimeData()
    payload.setHtml(html)
    payload.setText(text)
    return payload
