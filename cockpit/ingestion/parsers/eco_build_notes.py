"""ECO/Build Notes parser."""

import pathlib
import re

import docx
from docx.document import Document
from docx.table import _Cell
from docx.parts.document import DocumentPart

from ..errors import MalformedEcoError
from .results import EcoItem, EcoResult, EcoImageRef


CANONICAL_XRAY_HEADER = ["Find#", "PartNum", "Count", "Ref_Des", "Description"]

_LEADING_BULLET_RE = re.compile(r"^\s*\d+\s*[.)]\s+")


def _strip_leading_bullet(text: str) -> str:
    """Strip a hardcoded leading numeric bullet; keep text that is only a bullet."""
    stripped = _LEADING_BULLET_RE.sub("", text, count=1)
    return stripped if stripped else text


def collect_cell_image_refs(
    cell: _Cell,
    cell_index: int,
    document_part: DocumentPart
) -> list[EcoImageRef]:
    """Collect media parts referenced from one table cell in document order."""
    refs = []
    
    for elem in cell._element.iter():
        rel_id = None
        if elem.tag.endswith('}blip'):
            rel_id = elem.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
        elif elem.tag.endswith('}imagedata'):
            rel_id = elem.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            
        if rel_id and rel_id in document_part.related_parts:
            part = document_part.related_parts[rel_id]
            if hasattr(part, 'sha1') and hasattr(part, 'content_type'):
                refs.append(EcoImageRef(
                    blob_sha1=part.sha1,
                    content_type=part.content_type,
                    cell_index=cell_index,
                    order=len(refs)
                ))
    return refs


def parse(path: pathlib.Path) -> EcoResult:
    """Parse the ECO/Build Notes Word document into a unified checklist."""
    declared_part_number = path.name.split()[0].strip()

    try:
        doc = docx.Document(str(path))
    except Exception as e:
        raise MalformedEcoError(path, "UNREADABLE_DOCUMENT", {"error": str(e)})

    if len(doc.tables) > 3:
        raise MalformedEcoError(path, "TABLE_COUNT_DRIFT", {"expected": "<= 3", "observed": len(doc.tables)})

    items = []
    row_sequence = 1
    raw_table_count = len(doc.tables)

    build_tables = []
    xray_table = None

    for idx, tbl in enumerate(doc.tables):
        if len(tbl.rows) == 0:
            continue
            
        header_cells = [cell.text.strip() for cell in tbl.rows[0].cells]
        non_empty_cells = [c for c in header_cells if c]
        
        observed_xray_header = header_cells[:len(CANONICAL_XRAY_HEADER)]
        while len(observed_xray_header) < len(CANONICAL_XRAY_HEADER):
            observed_xray_header.append("")
            
        if observed_xray_header == CANONICAL_XRAY_HEADER:
            xray_table = (idx, tbl)
            continue
            
        build_tables.append((idx, tbl))

    document_part = doc.part

    for tbl_idx, tbl in build_tables:
        if len(tbl.rows) > 0:
            row0_cells = [cell.text.strip() for cell in tbl.rows[0].cells]
            non_empty_cells = [c for c in row0_cells if c]
            
            is_header = False
            if len(row0_cells) > 0 and row0_cells[0] == '#':
                is_header = True
            elif len(non_empty_cells) > 0 and non_empty_cells[0] in {"Find#", "Ref des", "Ref des (P/N)"}:
                is_header = True

            start_row = 1 if is_header else 0

            for r_idx in range(start_row, len(tbl.rows)):
                row = tbl.rows[r_idx]
                cell_texts = [cell.text.strip() for cell in row.cells]
                
                start_idx = 0
                while start_idx < len(cell_texts) and not cell_texts[start_idx]:
                    start_idx += 1
                    
                end_idx = len(cell_texts)
                while end_idx > start_idx and not cell_texts[end_idx - 1]:
                    end_idx -= 1
                    
                if start_idx == end_idx:
                    continue

                final_cells = list(cell_texts[start_idx:end_idx])
                
                if final_cells:
                    final_cells[0] = _strip_leading_bullet(final_cells[0])

                row_images = []
                for i in range(start_idx, end_idx):
                    returned_idx = i - start_idx
                    images = collect_cell_image_refs(row.cells[i], returned_idx, document_part)
                    row_images.extend(images)
                
                items.append(EcoItem(
                    row_sequence=row_sequence,
                    cells=tuple(final_cells),
                    images=tuple(row_images),
                    source_table_index=tbl_idx
                ))
                row_sequence += 1

    if xray_table is not None:
        tbl_idx, tbl = xray_table
        if len(tbl.rows) > 0:
            observed_header = [cell.text.strip() for cell in tbl.rows[0].cells][:len(CANONICAL_XRAY_HEADER)]
            while len(observed_header) < len(CANONICAL_XRAY_HEADER):
                observed_header.append("")
            if observed_header != CANONICAL_XRAY_HEADER:
                raise MalformedEcoError(path, "XRAY_HEADER_DRIFT", {
                    "expected": CANONICAL_XRAY_HEADER,
                    "observed": observed_header
                })

            for r_idx in range(1, len(tbl.rows)):
                row = tbl.rows[r_idx]
                cells_text = [cell.text.strip() for cell in row.cells]
                
                part_num = cells_text[1] if len(cells_text) > 1 else ""
                ref_des = cells_text[3] if len(cells_text) > 3 else ""
                description = cells_text[4] if len(cells_text) > 4 else ""
                
                if not part_num and not ref_des and not description:
                    continue
                    
                ref_des_cleaned = ref_des
                marker = "*please x-ray*"
                if marker in ref_des_cleaned.lower():
                    idx = ref_des_cleaned.lower().find(marker)
                    ref_des_cleaned = ref_des_cleaned[:idx] + ref_des_cleaned[idx+len(marker):]
                ref_des_cleaned = ref_des_cleaned.strip()
                
                final_cells = cells_text[:5]
                while len(final_cells) < 5:
                    final_cells.append("")
                final_cells[3] = ref_des_cleaned
                
                row_images = []
                for i in range(min(len(row.cells), 5)):
                    images = collect_cell_image_refs(row.cells[i], i, document_part)
                    row_images.extend(images)
                
                items.append(EcoItem(
                    row_sequence=row_sequence,
                    cells=tuple(final_cells),
                    images=tuple(row_images),
                    source_table_index=tbl_idx
                ))
                row_sequence += 1

    return EcoResult(
        declared_part_number=declared_part_number,
        items=items,
        raw_table_count=raw_table_count
    )
