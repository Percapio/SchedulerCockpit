"""Audit BOM parser."""

import pathlib
import openpyxl

from ..errors import MalformedBomError
from .results import BomItem, BomResult


CANONICAL_HEADER = [
    "Find#", "PartNum", "Count", "MSL level", "Date code", "Baked date", 
    "Ref_Des", "Package", "Description", "SMT/THT", "Qty Need", "Qty On hand", 
    "Qty short", "comment"
]


def parse(path: pathlib.Path) -> BomResult:
    """Parse the Audit BOM Excel file and extract THT items."""
    declared_part_number = path.name.split()[0].strip()

    try:
        # data_only=True to get values, not formulas
        wb = openpyxl.load_workbook(filename=str(path), data_only=True, read_only=True)
    except Exception as e:
        raise MalformedBomError(path, "UNREADABLE_WORKBOOK", {"error": str(e)})

    try:
        if "AUDIT BOM" not in wb.sheetnames:
            raise MalformedBomError(path, "MISSING_SHEET", {"expected": "AUDIT BOM", "available": wb.sheetnames})
            
        ws = wb["AUDIT BOM"]
        
        # Check header
        header_row = [cell.value for cell in ws[1]]
        # Pad or trim header to match canonical length for comparison
        observed_header = [str(v).strip() if v is not None else "" for v in header_row[:len(CANONICAL_HEADER)]]
        while len(observed_header) < len(CANONICAL_HEADER):
            observed_header.append("")
            
        if observed_header != CANONICAL_HEADER:
            raise MalformedBomError(path, "HEADER_DRIFT", {
                "expected": CANONICAL_HEADER, 
                "observed": observed_header
            })
            
        items = []
        raw_row_count = 0
        excluded_dni_count = 0
        
        for row in ws.iter_rows(min_row=2, values_only=True):
            raw_row_count += 1
            if len(row) < 10:
                continue
                
            flag = row[9]  # 0-indexed, so 9 is the 10th column ("SMT/THT")
            if flag is None:
                continue
                
            if str(flag).strip().upper().startswith('T'):
                part_number = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
                if part_number.upper() == "DNI":
                    excluded_dni_count += 1
                    continue
                    
                description = str(row[8]) if len(row) > 8 and row[8] is not None else None
                ref_des = str(row[6]) if len(row) > 6 and row[6] is not None else None
                
                items.append(BomItem(
                    component_mpn=part_number,
                    description=description,
                    ref_des=ref_des
                ))
                
        return BomResult(
            declared_part_number=declared_part_number,
            items=items,
            raw_row_count=raw_row_count,
            excluded_dni_count=excluded_dni_count
        )
    finally:
        wb.close()
