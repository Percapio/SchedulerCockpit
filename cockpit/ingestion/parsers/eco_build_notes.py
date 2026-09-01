"""ECO/Build Notes parser.

Reduced to its ingestion roles by Patch 08 §2: the declared part number that
cross_validation.reconcile checks against the BOM, and the drift gates that
reject a malformed document at drop time rather than at view time. Cell content
is no longer extracted -- the pane renders the .docx directly.
"""

import pathlib

import docx

from ..errors import MalformedEcoError
from .results import EcoResult

ACCEPTED_XRAY_HEADERS = (
    ("Find#", "PartNum", "Count", "Ref_Des", "Description"),
    ("Find#", "PartNum", "Ref_Des", "Description"),
)

MAX_TABLE_COUNT = 3


def _header_of(table) -> list[str]:
    if len(table.rows) == 0:
        return []
    return [cell.text.strip() for cell in table.rows[0].cells]


def is_accepted_xray_header(observed_header: list[str]) -> bool:
    return tuple(observed_header) in ACCEPTED_XRAY_HEADERS


def _looks_like_xray_table(header: list[str]) -> bool:
    """A table claiming to be the X-ray table without matching the header.

    Word documents in this family put Find# first in the X-ray table and
    nowhere else, so that column alone identifies the table well enough to
    hold it to the canonical header.
    """
    return bool(header) and header[0].strip() == "Find#"


def parse(path: pathlib.Path) -> EcoResult:
    """Validates an ECO/Build Notes document and reports its shape.

    pre:  path names a file reachable on disk
    post: returns the part number declared by the filename, the number of data
          rows across all tables, and the raw table count
    raises: MalformedEcoError with reason UNREADABLE_DOCUMENT, TABLE_COUNT_DRIFT
            or XRAY_HEADER_DRIFT
    """
    declared_part_number = path.name.split()[0].strip()

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise MalformedEcoError(path, "UNREADABLE_DOCUMENT", {"error": str(exc)})

    try:
        tables = document.tables
    except Exception as exc:
        raise MalformedEcoError(path, "UNREADABLE_DOCUMENT", {"error": str(exc)})

    raw_table_count = len(tables)
    if raw_table_count > MAX_TABLE_COUNT:
        raise MalformedEcoError(path, "TABLE_COUNT_DRIFT", {
            "expected": f"<= {MAX_TABLE_COUNT}",
            "observed": raw_table_count,
        })

    row_count = 0
    for table in tables:
        header = _header_of(table)
        if not header:
            continue

        if _looks_like_xray_table(header) and not is_accepted_xray_header(header):
            raise MalformedEcoError(path, "XRAY_HEADER_DRIFT", {
                "accepted": ACCEPTED_XRAY_HEADERS,
                "observed": header,
                "observed_column_count": len(header),
            })

        has_header_row = is_accepted_xray_header(header) or header[0].strip() in {"#", "Ref des", "Ref des (P/N)"}
        row_count += len(table.rows) - (1 if has_header_row else 0)

    return EcoResult(
        declared_part_number=declared_part_number,
        row_count=row_count,
        raw_table_count=raw_table_count,
    )
