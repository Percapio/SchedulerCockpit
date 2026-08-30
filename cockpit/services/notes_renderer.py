from dataclasses import dataclass
from typing import Generic, TypeVar, Literal
from pathlib import Path
import docx
from PyQt6.QtGui import (
    QTextDocument, QTextImageFormat, QTextCharFormat, QTextTableFormat, 
    QTextTableCellFormat, QColor, QImageReader, QImage, QFont, QTextCursor, QTextLength, QTextFrameFormat
)
from PyQt6.QtCore import QUrl, QSize, QBuffer, QByteArray

T = TypeVar('T')
E = TypeVar('E')

@dataclass
class Result(Generic[T, E]):
    ok: T | None = None
    err: E | None = None
    def is_ok(self) -> bool:
        return self.ok is not None

@dataclass
class RenderPalette:
    page_background_rgb: str
    default_text_rgb: str
    placeholder_border_rgb: str
    placeholder_text_rgb: str

AnomalyReason = Literal["UndecodableMedium", "DegenerateImage", "MergeConflict", "MediaLimitExceeded"]

@dataclass
class RenderAnomaly:
    reason: str
    relationship: str | None
    table_index: int
    row: int
    column: int

@dataclass
class RenderedNotes:
    document: QTextDocument
    natural_width: int
    anomalies: list[RenderAnomaly]

@dataclass
class NotesRenderFailure:
    reason: Literal["DocumentMissing", "DocumentUnreadable", "NoTablesFound"]
    message: str = ""

MAX_DOCUMENT_MEDIA_PX = 16_000_000

def _get_display_size(drawing, reader: QImageReader) -> QSize:
    extent = drawing.find('.//wp:extent', namespaces=drawing.nsmap)
    if extent is not None:
        cx = int(extent.get('cx', 0))
        cy = int(extent.get('cy', 0))
        if cx and cy:
            return QSize(int(cx / 9525), int(cy / 9525))
    return reader.size()

def _parse_table_widths(docx_table, cols_count):
    grid = docx_table._tbl.tblGrid
    widths = []
    if grid is not None:
        for gc in grid.gridCol_lst:
            if gc.w:
                widths.append(gc.w.twips / 15.0)
            else:
                widths.append(0)
    
    widths += [0] * max(0, cols_count - len(widths))
    widths = widths[:cols_count]
    
    autofit = docx_table.autofit
    specified = [w for w in widths if w > 0]
    
    if autofit or not specified:
        return [QTextLength(QTextLength.Type.PercentageLength, 100.0 / cols_count) for _ in range(cols_count)], 0
        
    if len(specified) == cols_count:
        return [QTextLength(QTextLength.Type.FixedLength, w) for w in widths], sum(widths)
        
    tblW_elem = docx_table._tbl.tblPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblW')
    tblW_px = 0
    if tblW_elem is not None:
        w_type = tblW_elem.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type')
        if w_type == 'dxa':
            tblW_px = int(tblW_elem.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}w', 0)) / 15.0
            
    unspecified_count = cols_count - len(specified)
    total_specified = sum(specified)
    
    if tblW_px > total_specified:
        fill_w = (tblW_px - total_specified) / unspecified_count
    else:
        fill_w = total_specified / len(specified) if specified else 0
        
    final_widths = []
    for w in widths:
        if w > 0:
            final_widths.append(QTextLength(QTextLength.Type.FixedLength, w))
        else:
            final_widths.append(QTextLength(QTextLength.Type.FixedLength, fill_w))
            
    natural = sum([l.rawValue() for l in final_widths])
    return final_widths, natural

def _get_borders(cell, table):
    borders = {}
    tcPr = cell._tc.get_or_add_tcPr()
    tcBorders = tcPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tcBorders')
    tblPr = table._tbl.tblPr
    tblBorders = tblPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblBorders')
    
    for edge in ('top', 'bottom', 'left', 'right'):
        edge_elem = None
        if tcBorders is not None:
            edge_elem = tcBorders.find(f'{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}{edge}')
        if edge_elem is not None:
            val = edge_elem.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val')
            borders[edge] = 'nil' if val in ('nil', 'none') else 'solid'
            continue
            
        if tblBorders is not None:
            edge_elem = tblBorders.find(f'{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}{edge}')
            if edge_elem is not None:
                val = edge_elem.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val')
                borders[edge] = 'nil' if val in ('nil', 'none') else 'solid'
                continue
                
        borders[edge] = 'nil'
    return borders

def render_build_notes(docx_path: Path, palette: RenderPalette) -> Result[RenderedNotes, NotesRenderFailure]:
    if not docx_path.exists():
        return Result(err=NotesRenderFailure("DocumentMissing"))
    
    try:
        doc = docx.Document(str(docx_path))
    except Exception as e:
        return Result(err=NotesRenderFailure("DocumentUnreadable", str(e)))
        
    if not doc.tables:
        return Result(err=NotesRenderFailure("NoTablesFound"))
        
    qdoc = QTextDocument()
    cursor = QTextCursor(qdoc)
    
    anomalies = []
    max_natural_width = 0
    total_media_px = 0
    
    for t_idx, docx_table in enumerate(doc.tables):
        rows = len(docx_table.rows)
        cols = len(docx_table.columns)
        if rows == 0 or cols == 0:
            continue
            
        t_fmt = QTextTableFormat()
        t_fmt.setBorder(0) # we use cell borders
        t_fmt.setCellSpacing(0)
        t_fmt.setCellPadding(4)
        
        col_widths, nat_width = _parse_table_widths(docx_table, cols)
        t_fmt.setColumnWidthConstraints(col_widths)
        if nat_width > max_natural_width:
            max_natural_width = nat_width
            
        qtable = cursor.insertTable(rows, cols, t_fmt)
        
        seen_tcs = set()
        h_merges = []
        active_vmerges = {}
        deferred_vmerges = []
        applied_merges = set() # store (r, c) of merged cells to check conflicts
        
        for r, row in enumerate(docx_table.rows):
            for c, cell in enumerate(row.cells):
                if cell._tc in seen_tcs:
                    continue
                seen_tcs.add(cell._tc)
                
                span = cell._tc.grid_span
                if span > 1:
                    h_merges.append((r, c, 1, span))
                    
                vmerge = cell._tc.vMerge
                if vmerge == 'restart':
                    active_vmerges[c] = r
                elif vmerge == 'continue':
                    if c in active_vmerges:
                        start_r = active_vmerges[c]
                        deferred_vmerges.append((start_r, c, r - start_r + 1, 1))
                    else:
                        anomalies.append(RenderAnomaly("MergeConflict", None, t_idx, r, c))
                    continue # don't render text for continue cells
                else:
                    if c in active_vmerges:
                        del active_vmerges[c]
                        
                # render cell content
                c_cursor = qtable.cellAt(r, c).firstCursorPosition()
                c_fmt = QTextTableCellFormat()
                borders = _get_borders(cell, docx_table)
                solid = QTextFrameFormat.BorderStyle.BorderStyle_Solid
                none = QTextFrameFormat.BorderStyle.BorderStyle_None
                c_fmt.setTopBorderStyle(solid if borders['top'] == 'solid' else none)
                c_fmt.setBottomBorderStyle(solid if borders['bottom'] == 'solid' else none)
                c_fmt.setLeftBorderStyle(solid if borders['left'] == 'solid' else none)
                c_fmt.setRightBorderStyle(solid if borders['right'] == 'solid' else none)
                
                tcPr = cell._tc.get_or_add_tcPr()
                shd = tcPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}shd')
                if shd is not None:
                    fill = shd.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fill')
                    if fill and fill != 'auto':
                        c_fmt.setBackground(QColor(f"#{fill}"))
                        
                vAlign = tcPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}vAlign')
                if vAlign is not None:
                    val = vAlign.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val')
                    if val == 'center':
                        c_fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignMiddle)
                    elif val == 'bottom':
                        c_fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignBottom)
                    else:
                        c_fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignTop)
                else:
                    c_fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignTop)
                    
                c_cursor.currentTable().cellAt(r, c).setFormat(c_fmt)
                
                # paragraphs
                for p_idx, paragraph in enumerate(cell.paragraphs):
                    if p_idx > 0:
                        c_cursor.insertBlock()
                    for run in paragraph.runs:
                        char_fmt = QTextCharFormat()
                        if run.bold: char_fmt.setFontWeight(QFont.Weight.Bold)
                        if run.italic: char_fmt.setFontItalic(True)
                        if run.underline: char_fmt.setFontUnderline(True)
                        if run.font.color and run.font.color.rgb:
                            char_fmt.setForeground(QColor(f"#{run.font.color.rgb}"))
                        else:
                            char_fmt.setForeground(QColor(palette.default_text_rgb))
                        if run.font.size:
                            char_fmt.setFontPointSize(run.font.size.pt)
                        if run.font.name:
                            char_fmt.setFontFamily(run.font.name)
                            
                        # iterate children
                        for child in run._r.iterchildren():
                            tag = child.tag.split('}')[-1]
                            if tag in ('t', 'tab', 'br', 'cr', 'noBreakHyphen', 'ptab'):
                                text = str(child)
                                if text:
                                    c_cursor.setCharFormat(char_fmt)
                                    c_cursor.insertText(text)
                            elif tag == 'drawing':
                                blip = child.find('.//a:blip', namespaces=child.nsmap)
                                if blip is not None:
                                    rel_id = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                                    if rel_id and rel_id in doc.part.related_parts:
                                        part = doc.part.related_parts[rel_id]
                                        c_type = getattr(part, 'content_type', '')
                                        
                                        if c_type not in ('image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/bmp'):
                                            anomalies.append(RenderAnomaly("UndecodableMedium", rel_id, t_idx, r, c))
                                            c_cursor.setCharFormat(char_fmt)
                                            c_cursor.insertText(f"[Unsupported Medium: {c_type}]")
                                            continue
                                            
                                        buf = QBuffer(QByteArray(part.blob))
                                        reader = QImageReader(buf)
                                        sz = _get_display_size(child, reader)
                                        
                                        if total_media_px + (sz.width() * sz.height()) > MAX_DOCUMENT_MEDIA_PX:
                                            anomalies.append(RenderAnomaly("MediaLimitExceeded", rel_id, t_idx, r, c))
                                            c_cursor.setCharFormat(char_fmt)
                                            c_cursor.insertText("[Image Limit Exceeded]")
                                            continue
                                            
                                        reader.setScaledSize(sz)
                                        img = reader.read()
                                        if not img.isNull() and img.width() > 0 and img.height() > 0:
                                            sha1 = getattr(part, 'sha1', rel_id)
                                            img_name = f"notes-img:{sha1}"
                                            qdoc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(img_name), img)
                                            
                                            i_fmt = QTextImageFormat()
                                            i_fmt.setName(img_name)
                                            i_fmt.setWidth(img.width())
                                            i_fmt.setHeight(img.height())
                                            c_cursor.insertImage(i_fmt)
                                            total_media_px += img.width() * img.height()
                                        else:
                                            anomalies.append(RenderAnomaly("DegenerateImage", rel_id, t_idx, r, c))
                                            c_cursor.setCharFormat(char_fmt)
                                            c_cursor.insertText("[Degenerate Image]")
                                            
        # apply merges
        def _overlap(r, c, rs, cs):
            for i in range(r, r + rs):
                for j in range(c, c + cs):
                    if (i, j) in applied_merges:
                        return True
            return False
            
        def _apply(r, c, rs, cs):
            for i in range(r, r + rs):
                for j in range(c, c + cs):
                    applied_merges.add((i, j))
            qtable.mergeCells(r, c, rs, cs)
            
        for r, c, rs, cs in h_merges:
            if not _overlap(r, c, rs, cs):
                _apply(r, c, rs, cs)
            else:
                anomalies.append(RenderAnomaly("MergeConflict", None, t_idx, r, c))
                
        # vmerges are overlapping sometimes? we check overlap
        # since we collected deferred vmerges, we need to sort and keep largest?
        # wait, deferred_vmerges might have same (start_r, c) with larger rowSpan because of multiple continue cells.
        # we only need to apply the largest one!
        vmerge_dict = {}
        for r, c, rs, cs in deferred_vmerges:
            if (r, c) not in vmerge_dict or vmerge_dict[(r, c)] < rs:
                vmerge_dict[(r, c)] = rs
                
        for (r, c), rs in vmerge_dict.items():
            if not _overlap(r, c, rs, 1):
                _apply(r, c, rs, 1)
            else:
                anomalies.append(RenderAnomaly("MergeConflict", None, t_idx, r, c))
                
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertBlock()
        
    return Result(ok=RenderedNotes(document=qdoc, natural_width=int(max_natural_width), anomalies=anomalies))
