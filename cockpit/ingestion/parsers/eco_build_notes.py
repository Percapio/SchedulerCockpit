"""ECO/Build Notes parser."""

import pathlib
import docx

from ..errors import MalformedEcoError
from .results import EcoItem, EcoResult


CANONICAL_XRAY_HEADER = ["Find#", "PartNum", "Count", "Ref_Des", "Description"]


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

    build_table = None
    xray_table = None

    # Identify tables by inspecting headers
    for idx, tbl in enumerate(doc.tables):
        if len(tbl.rows) == 0:
            continue
            
        header_cells = [cell.text.strip() for cell in tbl.rows[0].cells]
        non_empty_cells = [c for c in header_cells if c]
        
        # Check for X-Ray table first (stricter header)
        observed_xray_header = header_cells[:len(CANONICAL_XRAY_HEADER)]
        while len(observed_xray_header) < len(CANONICAL_XRAY_HEADER):
            observed_xray_header.append("")
            
        if observed_xray_header == CANONICAL_XRAY_HEADER:
            xray_table = (idx, tbl)
            continue
            
        # Check for Build Instructions
        if len(header_cells) > 0 and header_cells[0] == '#':
            build_table = (idx, tbl)
        elif len(non_empty_cells) > 0 and non_empty_cells[0] in {"Find#", "Ref des", "Ref des (P/N)"}:
            build_table = (idx, tbl)
        elif build_table is None:
            # If it doesn't have a recognized header but we haven't found a build table yet,
            # we might treat it as build table if it has rows, but let's stick to the heuristic.
            pass

    # --- Table 0 (Build Instructions) ---
    if build_table is not None:
        tbl_idx, tbl = build_table
        if len(tbl.rows) > 0:
            # Header sniff on row 0
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
                # Get cell texts, preserve newlines within cells
                cell_texts = [cell.text.strip() for cell in row.cells]
                
                # Drop empty leading cells
                while cell_texts and not cell_texts[0]:
                    cell_texts.pop(0)
                # Drop empty trailing cells
                while cell_texts and not cell_texts[-1]:
                    cell_texts.pop()
                    
                if not cell_texts:
                    continue

                # Join remaining cells with ' / '
                original_text = " / ".join(c for c in cell_texts if c)
                if original_text:
                    items.append(EcoItem(
                        row_sequence=row_sequence,
                        original_text=original_text,
                        source_table_index=tbl_idx
                    ))
                    row_sequence += 1

    # --- Table 1 (X-Ray Parts) ---
    if xray_table is not None:
        tbl_idx, tbl = xray_table
        if len(tbl.rows) > 0:
            # Header row is REQUIRED
            header_cells = [cell.text.strip() for cell in tbl.rows[0].cells]
            # Pad or trim to match canonical
            observed_header = header_cells[:len(CANONICAL_XRAY_HEADER)]
            while len(observed_header) < len(CANONICAL_XRAY_HEADER):
                observed_header.append("")
                
            if observed_header != CANONICAL_XRAY_HEADER:
                raise MalformedEcoError(path, "XRAY_HEADER_DRIFT", {
                    "expected": CANONICAL_XRAY_HEADER,
                    "observed": observed_header
                })
                
            for r_idx in range(1, len(tbl.rows)):
                row = tbl.rows[r_idx]
                cells = [cell.text.strip() for cell in row.cells]
                
                # Need at least PartNum(1), Ref_Des(3), Description(4)
                part_num = cells[1] if len(cells) > 1 else ""
                ref_des = cells[3] if len(cells) > 3 else ""
                description = cells[4] if len(cells) > 4 else ""
                
                if not part_num and not ref_des and not description:
                    continue
                    
                # Clean ref_des marker (case-insens)
                ref_des_cleaned = ref_des
                marker = "*please x-ray*"
                if marker in ref_des_cleaned.lower():
                    # Remove it using case-insensitive replace
                    idx = ref_des_cleaned.lower().find(marker)
                    ref_des_cleaned = ref_des_cleaned[:idx] + ref_des_cleaned[idx+len(marker):]
                ref_des_cleaned = ref_des_cleaned.strip()
                    
                original_text = f"X-ray {ref_des_cleaned} ({part_num}): {description}".strip()
                # Clean up double spaces if any component was empty
                original_text = original_text.replace(" ():", ":").replace("  ", " ")
                
                items.append(EcoItem(
                    row_sequence=row_sequence,
                    original_text=original_text,
                    source_table_index=tbl_idx
                ))
                row_sequence += 1

    return EcoResult(
        declared_part_number=declared_part_number,
        items=items,
        raw_table_count=raw_table_count
    )
