"""Parser result dataclasses."""

from dataclasses import dataclass
from typing import Any

from cockpit.persistence.types import ActiveAuditDraft


@dataclass(frozen=True)
class BomItem:
    component_mpn: str                  # PartNum column, stripped
    description: str | None             # Description column
    ref_des: str | None                 # Ref_Des column


@dataclass(frozen=True)
class BomResult:
    declared_part_number: str           # parsed from filename prefix
    items: list[BomItem]                # only THT rows that pass the DNI filter
    raw_row_count: int                  # total non-header rows scanned
    excluded_dni_count: int             # rows that matched startswith('T') but were filtered as DNI


@dataclass(frozen=True)
class EcoItem:
    row_sequence: int                   # monotonic across the entire ECO, starts at 1
    original_text: str                  # verbatim with embedded \n preserved
    source_table_index: int             # 0 (instructions) or 1 (x-ray); diagnostic only


@dataclass(frozen=True)
class EcoResult:
    declared_part_number: str           # from filename prefix
    items: list[EcoItem]
    raw_table_count: int


@dataclass(frozen=True)
class TravelerResult:
    sheet_name_used: str
    extracted_fields: dict[str, Any]    # field_key -> coerced value (str | int | datetime | None)
    raw_anchor_locations: dict[str, str]  # field_key -> cell coord actually used


@dataclass(frozen=True)
class IngestionIntent:
    audit_draft: ActiveAuditDraft           # ready for AuditRepository.create; carries traveler_metadata
    bom_items: list[BomItem]
    eco_items: list[EcoItem]
