"""Gatekeeper protocol implementation."""

import pathlib
from typing import Sequence

from .errors import GatekeeperViolation


def validate(paths: Sequence[pathlib.Path]) -> None:
    """Enforce exactly 3 or 4 files, where a 4th file must be a PDF."""
    if len(paths) not in (3, 4):
        raise GatekeeperViolation("WRONG_COUNT", {"actual": len(paths)})
        
    for p in paths:
        if not p.exists() or not p.is_file():
            if not p.exists():
                raise GatekeeperViolation("MISSING_FILE", {"path": str(p)})
            else:
                raise GatekeeperViolation("UNREADABLE_FILE", {"path": str(p)})
                
        ext = p.suffix.lower()
        if ext not in {".xlsx", ".csv", ".docx", ".pdf"}:
            raise GatekeeperViolation("UNSUPPORTED_EXTENSION", {"path": str(p), "extension": ext})

    bom_count = 0
    traveler_count = 0
    notes_count = 0
    pdf_count = 0

    for p in paths:
        name_lower = p.name.lower()
        ext = p.suffix.lower()
        if "audit bom" in name_lower:
            bom_count += 1
        elif "traveler" in name_lower:
            traveler_count += 1
        elif ext == ".docx":
            notes_count += 1
        elif ext == ".pdf":
            pdf_count += 1

    if bom_count == 0:
        raise GatekeeperViolation("MISSING_BOM", {})
    if bom_count > 1:
        raise GatekeeperViolation("AMBIGUOUS_BOM", {"count": bom_count})
        
    if traveler_count == 0:
        raise GatekeeperViolation("MISSING_TRAVELER", {})
    if traveler_count > 1:
        raise GatekeeperViolation("AMBIGUOUS_TRAVELER", {"count": traveler_count})
        
    if notes_count == 0:
        raise GatekeeperViolation("MISSING_NOTES_DOCX", {})
    if notes_count > 1:
        raise GatekeeperViolation("AMBIGUOUS_NOTES", {"count": notes_count})

    if len(paths) == 4:
        if pdf_count == 0:
            raise GatekeeperViolation("MISSING_PDF", {})
        if pdf_count > 1:
            raise GatekeeperViolation("AMBIGUOUS_PDF", {"count": pdf_count})
    else:
        if pdf_count > 0:
            raise GatekeeperViolation("WRONG_COUNT", {"actual": len(paths), "message": "PDF provided but total count is not 4"})
