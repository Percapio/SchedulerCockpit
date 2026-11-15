"""Persistence types (dataclasses and enums)."""
import pathlib
from dataclasses import dataclass
from datetime import datetime, date
from enum import StrEnum
from typing import Any


class AuditStatus(StrEnum):
    SHIPPING = "Shipping"
    THT = "THT"
    SMT = "SMT"
    FSU = "FSU"
    AOI = "AOI"
    OPS = "OPS"
    ON_HOLD = "ON HOLD"
    NOT_CLEAR = "Not Clear"

    @classmethod
    def ordered(cls) -> list['AuditStatus']:
        return [
            cls.SHIPPING,
            cls.OPS,
            cls.THT,
            cls.AOI,
            cls.SMT,
            cls.FSU,
            cls.ON_HOLD,
            cls.NOT_CLEAR
        ]


class SourceFileCategory(StrEnum):
    BOM = "BOM"
    TRAVELER = "Traveler"
    NOTES = "Notes"
    PDF = "PDF"
    SECONDARY_PDF = "SecondaryPDF"
    PLACEMENTS = "Placements"   # Phase 57: the stored extract of a job's .bmb


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
    created_at: datetime         # UTC, tz-aware
    updated_at: datetime         # UTC, tz-aware
    general_notes: str | None
    ship_date: str | None
    feeder_setuptime: float | None
    smt_runtime: float | None
    tht_runtime: float | None
    aoi_runtime: float | None
    ops_runtime: float | None
    shipping_runtime: float | None
    is_class_3: bool
    is_clean_process: bool
    ops_per_board_min: float | None = None
    # NULL and "FA" both mean first article. Not part of the identity key:
    # one family per part number and S/O is what makes replacement correct.
    article_revision: str | None = None
    is_labeled: bool = False
    are_photos_uploaded: bool = False
    xray_images_done: bool = False
    xray_images_done_task: str | None = None
    xray_images_done_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AuditStatus):
            object.__setattr__(self, 'status', AuditStatus(self.status))
        if not isinstance(self.xray_images_done, bool):
            object.__setattr__(self, 'xray_images_done', bool(self.xray_images_done))


@dataclass(frozen=True)
class SourceFile:
    id: int
    audit_id: int
    file_category: SourceFileCategory
    original_filename: str
    local_storage_path: pathlib.Path
    file_hash: str               # 64-char lowercase hex SHA-256 of the STORED bytes
    ingested_at: datetime        # UTC, tz-aware
    # Phase 57 §7.1: the share file's hash, size and modified time. For a copy, source_hash equals
    # file_hash; for a .bmb extract it is the whole share file's, and size and time are filled.
    source_hash: str = ""
    source_size: int | None = None
    source_modified_ns: int | None = None

    def __post_init__(self) -> None:
        if not self.source_hash:
            object.__setattr__(self, "source_hash", self.file_hash)


@dataclass(frozen=True)
class ThtChecklistItem:
    id: int
    audit_id: int
    source_file_id: int | None
    component_mpn: str
    description: str | None


@dataclass(frozen=True)
class ActiveAuditDraft:
    part_number: str
    work_order_ref: str
    quantity: int
    split_suffix: str = ""                              # '' for un-split
    schedule_job_id: int | None = None
    traveler_metadata: dict[str, Any] | None = None
    article_revision: str = "FA"


@dataclass(frozen=True)
class SourceFileDraft:
    audit_id: int
    file_category: SourceFileCategory
    original_filename: str
    local_storage_path: pathlib.Path
    file_hash: str                                       # caller pre-computes
    source_hash: str | None = None                       # None: the same as file_hash (a copy)
    source_size: int | None = None
    source_modified_ns: int | None = None


@dataclass(frozen=True)
class ThtChecklistItemDraft:
    audit_id: int
    component_mpn: str
    source_file_id: int | None = None
    description: str | None = None


@dataclass(frozen=True)
class AuditBomComponent:
    id: int
    source_file_id: int
    component_mpn: str
    ref_des: str
    mount_type: str  # Literal['T', 'S']
    description: str | None
    find_number: str


@dataclass(frozen=True)
class AuditBomComponentDraft:
    source_file_id: int
    component_mpn: str
    ref_des: str
    mount_type: str  # Literal['T', 'S']
    find_number: str
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
class PlacementRejection:
    """A .bmb that was rejected or refused for an audit (Phase 57 §7.3).

    reason is a BmbRejected or AdmissionRefused variant, or UnrecognisedReason for a kind a newer
    Cockpit wrote. bom_hash is the BOM a refusal was measured against; None for a file's rejection.
    """
    share_file_name: str
    share_hash: str
    share_size: int
    share_modified_ns: int
    reason: Any
    bom_hash: str | None
    seen_at: datetime


@dataclass(frozen=True)
class PdfComponentCoordDraft:
    source_file_id: int
    ref_des: str
    page_index: int
    x1: float
    y1: float
    x2: float
    y2: float
