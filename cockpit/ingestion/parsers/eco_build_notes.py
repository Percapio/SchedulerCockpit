"""ECO/Build Notes parser.

Reduced to its ingestion roles by Patch 08 §2: the declared part number that
cross_validation.reconcile checks against the BOM, and the drift gates that
reject a malformed document at drop time rather than at view time. Cell content
is no longer extracted -- the pane renders the .docx directly.
"""

import logging
import pathlib
from dataclasses import dataclass

import docx

from ..errors import MalformedEcoError
from .results import EcoResult

logger = logging.getLogger(__name__)

# Patch 12: presence, not shape. Any other column -- Count included -- is
# ignored, in any position.
REQUIRED_XRAY_COLUMNS = ("Find#", "PartNum", "Ref_Des", "Description")
XRAY_IDENTIFYING_COLUMN = "Find#"

MAX_TABLE_COUNT = 3


@dataclass(frozen=True)
class XrayHeaderAccepted:
    ignored_labels: list[str]


@dataclass(frozen=True)
class XrayHeaderRejected:
    missing: list[str]
    duplicated: list[str]


def header_key(raw_label: str) -> str:
    """Comparison key for a header label.

    post: casefolded, with every Unicode whitespace code point (including the
          non-breaking space Word inserts) and every "_" removed; "Ref Des",
          "REF_DES", " ref_des " and "Ref_Des" all map to "refdes"
    """
    return "".join(ch for ch in raw_label if not ch.isspace() and ch != "_").casefold()


_REQUIRED_KEYS = {header_key(label): label for label in REQUIRED_XRAY_COLUMNS}


def _header_of(table) -> list[str]:
    if len(table.rows) == 0:
        return []
    return [cell.text.strip() for cell in table.rows[0].cells]


def is_xray_table(observed_header: list[str]) -> bool:
    """A table whose first header cell is Find#, compared by header_key.

    Word documents in this family put Find# first in the X-ray table and
    nowhere else, so that column alone identifies the table well enough to
    hold it to the required columns.
    """
    return bool(observed_header) and header_key(observed_header[0]) == header_key(XRAY_IDENTIFYING_COLUMN)


def check_xray_header(observed_header: list[str]) -> XrayHeaderAccepted | XrayHeaderRejected:
    """Checks an X-ray header against the required column set.

    pre:  is_xray_table(observed_header); cells already stripped
    post: accepted iff every required column's key occurs exactly once, in any
          position and order; ignored_labels holds every other non-empty label
          verbatim, in source order. Rejected otherwise, with missing and
          duplicated in canonical labels and at least one of them non-empty
    """
    occurrences = {key: 0 for key in _REQUIRED_KEYS}
    ignored_labels: list[str] = []
    for label in observed_header:
        key = header_key(label)
        if key in occurrences:
            occurrences[key] += 1
        elif label:
            ignored_labels.append(label)

    missing = [_REQUIRED_KEYS[key] for key, count in occurrences.items() if count == 0]
    duplicated = [_REQUIRED_KEYS[key] for key, count in occurrences.items() if count > 1]
    if missing or duplicated:
        return XrayHeaderRejected(missing=missing, duplicated=duplicated)
    return XrayHeaderAccepted(ignored_labels=ignored_labels)


def parse(path: pathlib.Path) -> EcoResult:
    """Validates an ECO/Build Notes document and reports its shape.

    pre:  path names a file reachable on disk
    post: returns the part number declared by the filename, the number of data
          rows across all tables, and the raw table count; one INFO record per
          X-ray table that carries columns outside REQUIRED_XRAY_COLUMNS
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
    for table_index, table in enumerate(tables):
        header = _header_of(table)
        if not header:
            continue

        xray = is_xray_table(header)
        if xray:
            verdict = check_xray_header(header)
            if isinstance(verdict, XrayHeaderRejected):
                raise MalformedEcoError(path, "XRAY_HEADER_DRIFT", {
                    "required": REQUIRED_XRAY_COLUMNS,
                    "missing": verdict.missing,
                    "duplicated": verdict.duplicated,
                    "observed": header,
                    "observed_column_count": len(header),
                })
            if verdict.ignored_labels:
                logger.info(
                    "ECO %s table %d: ignoring X-ray columns %s",
                    path.name, table_index, verdict.ignored_labels,
                )

        has_header_row = xray or header[0].strip() in {"#", "Ref des", "Ref des (P/N)"}
        row_count += len(table.rows) - (1 if has_header_row else 0)

    return EcoResult(
        declared_part_number=declared_part_number,
        row_count=row_count,
        raw_table_count=raw_table_count,
    )
