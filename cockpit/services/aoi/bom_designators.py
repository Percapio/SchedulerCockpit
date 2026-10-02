"""Part names and DNI from the stored Audit BOM (Phase 52 section 3.2).

The BOM parser drops DNI lines before persisting, so the persisted components
cannot supply them. read_raw_rows keeps every row and already removes
struck-through text, so an ECO that struck a designator off one line leaves it
only where it still stands.
"""

import pathlib
import re
from typing import Mapping

from cockpit.ingestion.parsers.audit_bom import _ANNOTATION_RE, REFDES_TOKEN_REGEX, read_raw_rows

from .types import BomConflict, BomDesignatorMap, ConflictResolution

_PART_NUM = 1
_REF_DES = 3
_MIN_TRUNCATED_MATCH = 6


def _designators(ref_des_text: str) -> list[str]:
    cleaned = _ANNOTATION_RE.sub(" ", ref_des_text or "")
    return [t for t in re.split(r"[,\s]+", cleaned) if t and REFDES_TOKEN_REGEX.match(t)]


def _comparable(part_number: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", part_number.upper())


def _matches_cad(bom_part_number: str, cad_part_number: str) -> bool:
    bom, cad = _comparable(bom_part_number), _comparable(cad_part_number)
    if not bom or not cad:
        return False
    # The customer XY truncates long part numbers (24 characters in the corpus).
    return bom == cad or (len(cad) >= _MIN_TRUNCATED_MATCH and bom.startswith(cad))


def resolve_designator(
    ref_des: str,
    part_numbers: list[str],
    cad_part_number: str | None,
) -> tuple[str, BomConflict | None]:
    """Chooses one PartNum for a designator listed on one or more BOM lines.

    post: a single distinct part number is returned with no conflict; otherwise
          DNI wins, then the line matching the CAD PartNum, then the first line
          in sheet order, and the choice is reported as a BomConflict
    """
    distinct: list[str] = []
    for part in part_numbers:
        if all(_comparable(part) != _comparable(seen) for seen in distinct):
            distinct.append(part)
    if len(distinct) == 1:
        return distinct[0], None

    if any(part.upper() == "DNI" for part in distinct):
        chosen, resolution = "DNI", ConflictResolution.DNI_LINE
    else:
        matching = [p for p in distinct if cad_part_number and _matches_cad(p, cad_part_number)]
        if matching:
            chosen, resolution = matching[0], ConflictResolution.CAD_MATCH
        else:
            chosen, resolution = distinct[0], ConflictResolution.FIRST_LINE
    return chosen, BomConflict(ref_des, tuple(distinct), chosen, resolution)


def read_bom_designators(
    stored_bom: pathlib.Path,
    cad_part_numbers: Mapping[str, str],
) -> BomDesignatorMap:
    """Maps every designator on the stored Audit BOM to one PartNum, DNI included.

    pre:  stored_bom is the audit's BOM source file in managed storage
    post: one PartNum per designator per resolve_designator; every designator
          with differing part numbers on several lines is listed in conflicts
    raises: MalformedBomError, OSError
    """
    listings: dict[str, list[str]] = {}
    for row in read_raw_rows(stored_bom):
        part_number = row.cells[_PART_NUM].strip()
        if not part_number:
            continue
        for ref_des in _designators(row.cells[_REF_DES]):
            listings.setdefault(ref_des, []).append(part_number)

    names: dict[str, str] = {}
    conflicts: list[BomConflict] = []
    for ref_des, parts in listings.items():
        chosen, conflict = resolve_designator(ref_des, parts, cad_part_numbers.get(ref_des))
        names[ref_des] = chosen
        if conflict is not None:
            conflicts.append(conflict)
    return BomDesignatorMap(names, tuple(conflicts))
