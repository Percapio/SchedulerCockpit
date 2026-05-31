"""View-layer dataclasses for the UI."""

from dataclasses import dataclass, replace, field
from datetime import datetime, date
from enum import StrEnum
from typing import Any, Sequence
import pathlib

from cockpit.persistence.types import AuditStatus


class ChecklistRowKind(StrEnum):
    THT = "tht"
    NOTES = "notes"


@dataclass(frozen=True)
class ChecklistRowKey:
    kind: ChecklistRowKind
    item_id: int


class SelectionKind(StrEnum):
    THT_MPN = "tht_mpn"
    MPN_CELL = "mpn_cell"
    BOM_MPN = "bom_mpn"
    CLEAR = "clear"


class ResolutionKind(StrEnum):
    SINGLE_REFDES = "single_refdes"
    MULTI_REFDES = "multi_refdes"
    ABSENT_FROM_PDF = "absent_from_pdf"
    NO_PDF = "no_pdf"
    UNKNOWN_MPN = "unknown_mpn"
    GROUP_REFDES = "group_refdes"
    GROUP_ABSENT = "group_absent"
    MULTI_MPN_GROUP = "multi_mpn_group"
    MULTI_MPN_GROUP_ABSENT = "multi_mpn_group_absent"


@dataclass(frozen=True)
class SelectionIntent:
    kind: SelectionKind
    mpn: str | None = None

    def __post_init__(self) -> None:
        if self.kind in (SelectionKind.THT_MPN, SelectionKind.MPN_CELL, SelectionKind.BOM_MPN):
            if not self.mpn:
                raise ValueError("mpn must be populated")
        elif self.kind == SelectionKind.CLEAR:
            if self.mpn is not None:
                raise ValueError("mpn must be None for CLEAR")


@dataclass(frozen=True)
class HighlightCoord:
    ref_des: str
    page_index: int
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class RefDesLocation:
    mpn: str
    mount_type: str


@dataclass(frozen=True)
class ResolvedSelection:
    kind: ResolutionKind
    mpn: str | None
    mpn_set: frozenset[str] | None
    ref_des_list: tuple[str, ...]
    coords: tuple[HighlightCoord, ...]

    def __post_init__(self) -> None:
        is_multi = self.kind in (ResolutionKind.MULTI_MPN_GROUP, ResolutionKind.MULTI_MPN_GROUP_ABSENT)
        if is_multi:
            if self.mpn is not None:
                raise ValueError("mpn must be None for MULTI_MPN_GROUP kinds")
            if not self.mpn_set:
                raise ValueError("mpn_set must be populated for MULTI_MPN_GROUP kinds")
        else:
            if not self.mpn:
                raise ValueError("mpn must be populated for non-multi kinds")
            if self.mpn_set is not None:
                raise ValueError("mpn_set must be None for non-multi kinds")


@dataclass(frozen=True)
class LayoutContext:
    """The canvas's view of one audit's PDF state."""
    audit_id: int
    pdf_source_file_id: int | None
    pdf_path: pathlib.Path | None
    page_count: int
    page_dimensions: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if self.pdf_path is None:
            if self.pdf_source_file_id is not None:
                raise ValueError("pdf_source_file_id must be None when pdf_path is None")
            if self.page_count != 0:
                raise ValueError("page_count must be 0 when pdf_path is None")
            if self.page_dimensions != ():
                raise ValueError("page_dimensions must be empty when pdf_path is None")
        else:
            if self.pdf_source_file_id is None:
                raise ValueError("pdf_source_file_id must not be None when pdf_path is not None")
            if self.page_count not in {1, 2}:
                raise ValueError("page_count must be 1 or 2 when pdf_path is not None")
            if len(self.page_dimensions) != self.page_count:
                raise ValueError("len(page_dimensions) must equal page_count")


@dataclass(frozen=True)
class ChecklistRowView:
    key: ChecklistRowKey
    primary_label: str
    secondary_label: str | None = None
    is_verified: bool = False
    find_number: int | None = None
    ref_des_list: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActiveAuditView:
    audit_id: int
    part_number: str
    work_order_ref: str
    split_suffix: str
    quantity: int
    status: AuditStatus
    split_reason: str | None
    traveler_metadata: dict[str, Any] | None
    ship_date: date | None
    has_pdf: bool
    tht_placement_count: int = 0
    tht_rows: Sequence[ChecklistRowView] = field(default_factory=tuple)
    notes_rows: Sequence[ChecklistRowView] = field(default_factory=tuple)

    @property
    def total_rows(self) -> int:
        return len(self.tht_rows) + len(self.notes_rows)

    @property
    def verified_rows(self) -> int:
        return sum(1 for r in self.tht_rows if r.is_verified) + \
               sum(1 for r in self.notes_rows if r.is_verified)

    @property
    def is_fully_verified(self) -> bool:
        return self.verified_rows == self.total_rows

    def with_row_replaced(self, updated: ChecklistRowView) -> "ActiveAuditView":
        tht = list(self.tht_rows)
        notes = list(self.notes_rows)
        
        target = tht if updated.key.kind == ChecklistRowKind.THT else notes
        
        for i, row in enumerate(target):
            if row.key == updated.key:
                target[i] = updated
                break
        else:
            raise KeyError(f"Row {updated.key} not found in view")
            
        return replace(self, tht_rows=tht, notes_rows=notes)

    def with_ship_date(self, new_value: date | None) -> "ActiveAuditView":
        return replace(self, ship_date=new_value)


@dataclass(frozen=True)
class SplitSummary:
    sibling_suffix: str
    sibling_quantity: int


@dataclass(frozen=True)
class OpenAuditDigest:
    audit_id: int
    part_number: str
    work_order_ref: str
    split_suffix: str
    quantity: int
    status: AuditStatus
    updated_at: datetime


import pathlib
from cockpit.persistence.errors import PersistenceError

@dataclass(frozen=True)
class ReapReport:
    deleted_paths: list[pathlib.Path]
    retained_files: list[tuple[pathlib.Path, str]]
    failed_paths: list[tuple[pathlib.Path, str]]
    pruned_directories: list[pathlib.Path]


@dataclass(frozen=True)
class CompletionOutcome:
    audit_id: int
    reap_report: ReapReport


@dataclass(frozen=True)
class ReconciliationReport:
    cleaned: list[CompletionOutcome]
    partial: list[tuple[int, ReapReport]]
    errors: list[tuple[int, PersistenceError]]
    orphans_deleted: list[pathlib.Path]
    orphan_delete_failed: list[tuple[pathlib.Path, Exception]]
    unreadable: list[tuple[pathlib.Path, Exception]]
    pruned: list[pathlib.Path]
    notes: list[str]
