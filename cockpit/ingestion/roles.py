"""Role classification for ingested files."""

from enum import Enum
import pathlib


class SourceRole(Enum):
    BOM = "BOM"
    TRAVELER = "TRAVELER"
    NOTES = "NOTES"
    PDF = "PDF"
    UNKNOWN = "UNKNOWN"


def role_of(file_name: str) -> SourceRole:
    """The role a filename declares.
    
    Exactly one role, by the ordered chain in the table below. Matching is
    case-insensitive and runs against the file's name with its extension
    included. The first matching rule wins; a name matching none is UNKNOWN.
    """
    name_lower = file_name.lower()
    
    if "audit bom" in name_lower:
        return SourceRole.BOM
    if "traveler" in name_lower:
        return SourceRole.TRAVELER
    
    ext = pathlib.Path(file_name).suffix.lower()
    
    if ext == ".docx":
        return SourceRole.NOTES
    if ext == ".pdf":
        return SourceRole.PDF
        
    return SourceRole.UNKNOWN
