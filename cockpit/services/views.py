"""View-layer dataclasses for the UI."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from cockpit.persistence.types import AuditStatus


class ChecklistRowKind(StrEnum):
    THT = "tht"
    NOTES = "notes"


@dataclass(frozen=True)
class ChecklistRowKey:
    kind: ChecklistRowKind
    item_id: int


@dataclass(frozen=True)
class ChecklistRowView:
    key: ChecklistRowKey
    primary_label: str
    secondary_label: str | None
    is_verified: bool
    notes: str | None


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
    tht_rows: list[ChecklistRowView]
    notes_rows: list[ChecklistRowView]

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
