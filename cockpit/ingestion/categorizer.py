"""File categorizer module."""

import pathlib
from dataclasses import dataclass
from typing import Sequence

from .errors import CategorizationError


@dataclass(frozen=True)
class CategorizedQuartet:
    bom_path: pathlib.Path
    traveler_path: pathlib.Path
    notes_path: pathlib.Path
    pdf_path: pathlib.Path | None = None


def categorize(paths: Sequence[pathlib.Path]) -> CategorizedQuartet:
    """Assign each path in the validated trio/quartet to a role."""
    from .roles import role_of, SourceRole

    bom_path = None
    traveler_path = None
    notes_path = None
    pdf_path = None

    for p in paths:
        role = role_of(p.name)
        if role == SourceRole.BOM:
            bom_path = p
        elif role == SourceRole.TRAVELER:
            traveler_path = p
        elif role == SourceRole.NOTES:
            notes_path = p
        elif role == SourceRole.PDF:
            pdf_path = p

    if not bom_path:
        raise CategorizationError(paths[0], "No BOM found during categorization")
    if not traveler_path:
        raise CategorizationError(paths[0], "No Traveler found during categorization")
    if not notes_path:
        raise CategorizationError(paths[0], "No Notes found during categorization")

    return CategorizedQuartet(
        bom_path=bom_path,
        traveler_path=traveler_path,
        notes_path=notes_path,
        pdf_path=pdf_path
    )
