"""Persistence types (dataclasses and enums)."""
import pathlib
from dataclasses import dataclass
from datetime import datetime, date
from enum import StrEnum
from typing import Any


class AuditStatus(StrEnum):
    PENDING = "Pending"
    IN_PROGRESS = "InProgress"
    COMPLETED = "Completed"


class SourceFileCategory(StrEnum):
    BOM = "BOM"
    TRAVELER = "Traveler"
    NOTES = "Notes"
    PDF = "PDF"


# ---------- row types (returned by repositories) ----------

@dataclass(frozen=True)
class ActiveAudit:
    id: int
    part_number: str
    schedule_job_id: int | None
    work_order_ref: str
    split_suffix: str            # '' when un-split; never None
    quantity: int
    status: AuditStatus
    split_reason: str | None
    traveler_metadata: dict[str, Any] | None
    ship_date: date | None       # NEW (Phase 6)
    created_at: datetime         # UTC, tz-aware
    updated_at: datetime         # UTC, tz-aware


@dataclass(frozen=True)
class SourceFile:
    id: int
    audit_id: int
    file_category: SourceFileCategory
    original_filename: str
    local_storage_path: pathlib.Path
    file_hash: str               # 64-char lowercase hex SHA-256
    ingested_at: datetime        # UTC, tz-aware


@dataclass(frozen=True)
class ThtChecklistItem:
    id: int
    audit_id: int
    source_file_id: int | None
    component_mpn: str
    description: str | None
    is_verified: bool


@dataclass(frozen=True)
class BuildNoteItem:
    id: int
    audit_id: int
    source_file_id: int | None
    row_sequence: int
    original_text: str
    is_verified: bool


# ---------- draft types (consumed by repositories on insert) ----------

@dataclass(frozen=True)
class ActiveAuditDraft:
    part_number: str
    work_order_ref: str
    quantity: int
    split_suffix: str = ""                              # '' for un-split
    schedule_job_id: int | None = None
    traveler_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class SourceFileDraft:
    audit_id: int
    file_category: SourceFileCategory
    original_filename: str
    local_storage_path: pathlib.Path
    file_hash: str                                       # caller pre-computes


@dataclass(frozen=True)
class ThtChecklistItemDraft:
    audit_id: int
    component_mpn: str
    source_file_id: int | None = None
    description: str | None = None
    is_verified: bool = False


@dataclass(frozen=True)
class BuildNoteItemDraft:
    audit_id: int
    row_sequence: int
    original_text: str
    source_file_id: int | None = None
    is_verified: bool = False


@dataclass(frozen=True)
class AuditBomComponent:
    id: int
    source_file_id: int
    component_mpn: str
    ref_des: str
    mount_type: str  # Literal['T', 'S']
    description: str | None
    find_number: int


@dataclass(frozen=True)
class AuditBomComponentDraft:
    source_file_id: int
    component_mpn: str
    ref_des: str
    mount_type: str  # Literal['T', 'S']
    find_number: int
    description: str | None = None


@dataclass(frozen=True)
class PdfComponentCoord:
    id: int
    source_file_id: int
    ref_des: str
    page_index: int
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class PdfComponentCoordDraft:
    source_file_id: int
    ref_des: str
    page_index: int
    x1: float
    y1: float
    x2: float
    y2: float
