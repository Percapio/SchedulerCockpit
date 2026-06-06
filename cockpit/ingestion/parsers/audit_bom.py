"""Audit BOM parser."""

import pathlib
import re
import openpyxl
from typing import Any

from ..errors import MalformedBomError
from ..filename_rules import derive_part_number_from_filename
from .results import BomItem, BomResult


_ANNOTATION_RE = re.compile(
    r'\*[^*]*\*'        # asterisk markers      (existing)
    r'|\([^)]*\)'       # parenthesis wrappers  (NEW)
    r'|\{[^}]*\}'       # curly-brace wrappers   (NEW)
    r'|\[[^\]]*\]'      # square-bracket wrappers (NEW)
)

REFDES_TOKEN_REGEX = re.compile(r"^[A-Za-z0-9_.+-]+$")

CANONICAL_HEADER = [
    "Find#", "PartNum", "Count", "MSL level", "Date code", "Baked date", 
    "Ref_Des", "Package", "Description", "SMT/THT", "Qty Need", "Qty On hand", 
    "Qty short", "comment"
]


def normalize_sheet_label(label: str) -> str:
    """Normalize sheet label for case-insensitive matching."""
    return str(label).strip().casefold()


def is_valid_excel_sheet_name(candidate: str) -> bool:
    """Check if the given string could be a valid Excel worksheet name."""
    if not candidate:
        return False
    if len(candidate) > 31:
        return False
    for c in ":\\/?*[]":
        if c in candidate:
            return False
    return True


def choose_bom_sheet(path: pathlib.Path, available_sheet_names: list[str], declared_part_number: str) -> str:
    """Select the worksheet to parse, prioritizing canonical over assembly name."""
    available_norms = {normalize_sheet_label(n): n for n in available_sheet_names}
    
    canon_norm = normalize_sheet_label("AUDIT BOM")
    if canon_norm in available_norms:
        return available_norms[canon_norm]
        
    if not is_valid_excel_sheet_name(declared_part_number):
        raise MalformedBomError(path, "MISSING_SHEET", {
            "expected_canonical": "AUDIT BOM",
            "expected_assembly": None,
            "available": available_sheet_names
        })
        
    decl_norm = normalize_sheet_label(declared_part_number)
    if decl_norm in available_norms:
        return available_norms[decl_norm]
        
    raise MalformedBomError(path, "MISSING_SHEET", {
        "expected_canonical": "AUDIT BOM",
        "expected_assembly": declared_part_number,
        "available": available_sheet_names
    })


def coerce_find_number(raw: Any, path: pathlib.Path, mpn: str) -> int:
    if raw is None or str(raw).strip() == "":
        raise MalformedBomError(path, "MISSING_FIND_NUMBER", {"mpn": mpn})
    try:
        fval = float(raw)
        ival = int(fval)
        if fval != ival or ival < 1:
            raise MalformedBomError(path, "INVALID_FIND_NUMBER", {"mpn": mpn, "raw": raw})
        return ival
    except (ValueError, TypeError):
        raise MalformedBomError(path, "INVALID_FIND_NUMBER", {"mpn": mpn, "raw": raw})


def _split_ref_des(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    # Strip annotation markers before delimiter inspection so that hyphens
    # inside markers (e.g. "*PLEASE X-RAY*") do not trigger the guard.
    cleaned = _ANNOTATION_RE.sub("", raw).strip()
    if not cleaned:
        return ()

    tokens = [tok.strip() for tok in cleaned.split(",") if tok.strip()]
    for tok in tokens:
        if not REFDES_TOKEN_REGEX.match(tok):
            raise ValueError("INVALID_REFDES_TOKEN")
    return tuple(tokens)


def parse(path: pathlib.Path) -> BomResult:
    """Parse the Audit BOM Excel file and extract THT items."""
    declared_part_number = derive_part_number_from_filename(path)

    try:
        # data_only=True to get values, not formulas
        wb = openpyxl.load_workbook(filename=str(path), data_only=True, read_only=True)
    except Exception as e:
        raise MalformedBomError(path, "UNREADABLE_WORKBOOK", {"error": str(e)})

    try:
        selected_sheet_name = choose_bom_sheet(path, wb.sheetnames, declared_part_number)
        ws = wb[selected_sheet_name]
        
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
        excluded_pcb_count = 0
        
        seen_mpns = set()
        duplicate_mpns = set()
        
        for row in ws.iter_rows(min_row=2, values_only=True):
            raw_row_count += 1
            if len(row) < 10:
                continue
                
            flag = row[9]  # 0-indexed, so 9 is the 10th column ("SMT/THT")
            if flag is None:
                continue
                
            flag_str = str(flag).strip().upper()
            if flag_str.startswith('T'):
                mount_type = 'T'
            elif flag_str.startswith('S'):
                mount_type = 'S'
            else:
                continue
                
            part_number = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
            if not part_number:
                continue
                
            if part_number.upper() == "DNI":
                excluded_dni_count += 1
                continue
            if part_number.upper() == "PCB":
                excluded_pcb_count += 1
                continue
                
            find_number = coerce_find_number(row[0], path, part_number)
                
            description = str(row[8]) if len(row) > 8 and row[8] is not None else None
            ref_des_raw = str(row[6]) if len(row) > 6 and row[6] is not None else None
            
            try:
                ref_des_list = _split_ref_des(ref_des_raw)
            except ValueError as ve:
                raise MalformedBomError(path, str(ve), {"raw": ref_des_raw, "mpn": part_number})
                
            if part_number in seen_mpns:
                duplicate_mpns.add(part_number)
            seen_mpns.add(part_number)
            
            items.append(BomItem(
                find_number=find_number,
                component_mpn=part_number,
                description=description,
                ref_des_raw=ref_des_raw,
                ref_des_list=ref_des_list,
                mount_type=mount_type
            ))
            
        if duplicate_mpns:
            raise MalformedBomError(path, "DUPLICATE_MPN", {"duplicates": list(duplicate_mpns)})
            
        return BomResult(
            declared_part_number=declared_part_number,
            items=items,
            raw_row_count=raw_row_count,
            excluded_dni_count=excluded_dni_count,
            excluded_pcb_count=excluded_pcb_count
        )
    finally:
        wb.close()
