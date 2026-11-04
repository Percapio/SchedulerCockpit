"""Parser result dataclasses."""

from dataclasses import dataclass, field
from typing import Any, Literal

from cockpit.persistence.types import ActiveAuditDraft

HeaderLayout = Literal["canonical", "legacy"]


@dataclass(frozen=True)
class BomItem:
    find_number: str                    # Find# column (row[0]); BOM line identifier
    component_mpn: str                  # PartNum column, stripped
    description: str | None             # Description column
    ref_des_raw: str | None             # Ref_Des column (original text)
    ref_des_list: tuple[str, ...]       # Split Ref_Des values
    mount_type: Literal['T', 'S']       # MountType column


@dataclass(frozen=True)
class BomResult:
    declared_part_number: str           # parsed from filename prefix
    items: list[BomItem]                # All items (THT and SMT)
    raw_row_count: int                  # total non-header rows scanned
    excluded_dni_count: int             # rows that were filtered as DNI
    excluded_pcb_count: int             # rows that were excluded as PCBs
    header_layout: HeaderLayout         # "canonical" (all optionals present) or "legacy"
    notes: list[Any] = field(default_factory=list)
    cleaning: Any = None
    sheet_name_used: str = ""
    header_row: int = 1


@dataclass(frozen=True)
class EcoResult:
    declared_part_number: str           # from filename prefix
    row_count: int
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
