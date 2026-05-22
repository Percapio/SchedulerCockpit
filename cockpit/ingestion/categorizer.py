"""File categorizer module."""

import pathlib
from dataclasses import dataclass
from typing import Sequence

from .errors import CategorizationError


@dataclass(frozen=True)
class CategorizedTrio:
    bom_path: pathlib.Path
    traveler_path: pathlib.Path
    notes_path: pathlib.Path


def categorize(paths: Sequence[pathlib.Path]) -> CategorizedTrio:
    """Assign each path in the validated trio to a role."""
    bom_path = None
    traveler_path = None
    notes_path = None

    for p in paths:
        name_lower = p.name.lower()
        if "audit bom" in name_lower:
            bom_path = p
        elif "traveler" in name_lower:
            traveler_path = p
        elif p.suffix.lower() == ".docx":
            notes_path = p

    if not bom_path:
        raise CategorizationError(paths[0], "No BOM found during categorization")
    if not traveler_path:
        raise CategorizationError(paths[0], "No Traveler found during categorization")
    if not notes_path:
        raise CategorizationError(paths[0], "No Notes found during categorization")

    return CategorizedTrio(
        bom_path=bom_path,
        traveler_path=traveler_path,
        notes_path=notes_path
    )
