"""Interchange types for the AOI XY export (Phase 52 section 3.0)."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Union


class BoardSide(Enum):
    TOP = "T"
    BOTTOM = "B"


class ProgramFormat(Enum):
    E42 = "E42"
    E45 = "E45"
    E46 = "E46"


class FrameStatus(Enum):
    """Where a side's coordinates are measured from."""
    PROGRAM_ORIGIN = "PROGRAM_ORIGIN"
    CAD_ORIGIN = "CAD_ORIGIN"


class FiducialStatus(Enum):
    """Where a side's fiducials came from. Orthogonal to FrameStatus."""
    PROGRAM_MARKS = "PROGRAM_MARKS"
    CAD_ROWS = "CAD_ROWS"
    NONE = "NONE"


# --- customer XY -------------------------------------------------------------

@dataclass(frozen=True)
class CadPlacement:
    ref_des: str
    part_number: str
    x_mil: float
    y_mil: float
    rotation_deg: float
    side: BoardSide
    source_line: int


@dataclass(frozen=True)
class CadFiducial:
    """A fiducial row in the customer XY (FIDTOP1, FID2...); it has no rotation."""
    ref_des: str
    x_mil: float
    y_mil: float
    side: BoardSide
    source_line: int


@dataclass(frozen=True)
class XyrsData:
    path: Path
    placements: tuple[CadPlacement, ...]
    fiducials: tuple[CadFiducial, ...]


@dataclass(frozen=True)
class XyLimits:
    max_bytes: int = 8 * 1024 * 1024
    max_rows: int = 50_000
    excluded_designators: frozenset[str] = frozenset({"PCB"})


@dataclass(frozen=True)
class XyLocated:
    path: Path


@dataclass(frozen=True)
class XyPending:
    candidates: tuple[Path, ...]


@dataclass(frozen=True)
class XyNotFound:
    searched: Path


XyLocateOutcome = Union[XyLocated, XyPending, XyNotFound]


# --- Juki programs -----------------------------------------------------------

@dataclass(frozen=True)
class ProgramFile:
    path: Path
    side: BoardSide
    split_index: Optional[int]
    article: Optional[str]
    format: ProgramFormat


@dataclass(frozen=True)
class ProgramsLocated:
    folder: Path
    files: tuple[ProgramFile, ...]


@dataclass(frozen=True)
class ProgramArticlePending:
    """Programs for more than one article build sit in the job's program folder."""
    folder: Path
    by_article: dict[Optional[str], tuple[ProgramFile, ...]]

    def articles(self) -> list[Optional[str]]:
        return sorted(self.by_article, key=lambda a: (a is not None, a or ""))

    def choose(self, article: Optional[str]) -> ProgramsLocated:
        return ProgramsLocated(self.folder, self.by_article[article])


@dataclass(frozen=True)
class ProgramFolderNotFound:
    searched: Path


ProgramLocateOutcome = Union[ProgramsLocated, ProgramArticlePending, ProgramFolderNotFound]


@dataclass(frozen=True)
class ProgramLimits:
    max_bytes: int = 8 * 1024 * 1024


@dataclass(frozen=True)
class MachinePlacement:
    """One placement record. Units: 0.1 um (254 per mil); angle in 1/80 degree."""
    ref_des: str
    x_units: int
    y_units: int
    angle_units: int


@dataclass(frozen=True)
class FiducialMark:
    x_units: int
    y_units: int


@dataclass(frozen=True)
class DecodedProgram:
    file: ProgramFile
    placements: dict[str, MachinePlacement]
    # None: this format's mark table is not known. Empty: known, and holds none.
    marks: Optional[tuple[FiducialMark, ...]]


@dataclass(frozen=True)
class FrameTolerance:
    min_matches: int = 5
    max_residual_mil: float = 1.0
    min_inlier_ratio: float = 0.9


@dataclass(frozen=True)
class ProgramFrame:
    """AOI = CAD + offset; the program stores (s * AOI_x, AOI_y) x 254, s = -1 if mirrored."""
    offset_x_mil: float
    offset_y_mil: float
    mirrored_x: bool
    matched: int
    max_residual_mil: float
    outliers: tuple[str, ...] = ()


# --- Audit BOM -----------------------------------------------------------------

class ConflictResolution(Enum):
    DNI_LINE = "DNI_LINE"
    CAD_MATCH = "CAD_MATCH"
    FIRST_LINE = "FIRST_LINE"


@dataclass(frozen=True)
class BomConflict:
    ref_des: str
    part_numbers: tuple[str, ...]
    chosen: str
    resolution: ConflictResolution


@dataclass(frozen=True)
class BomDesignatorMap:
    names: dict[str, str]
    conflicts: tuple[BomConflict, ...] = ()


# --- AOI documents -------------------------------------------------------------

class FlagKind(Enum):
    BOM_CONFLICT = "BOM_CONFLICT"
    MISSING_FROM_BOM = "MISSING_FROM_BOM"
    PROGRAM_SKIPPED = "PROGRAM_SKIPPED"
    FRAME_UNAVAILABLE = "FRAME_UNAVAILABLE"
    FRAME_OUTLIERS = "FRAME_OUTLIERS"
    MARKS_UNAVAILABLE = "MARKS_UNAVAILABLE"
    MARKS_DISAGREE = "MARKS_DISAGREE"


@dataclass(frozen=True)
class AoiFlag:
    kind: FlagKind
    message: str
    designators: tuple[str, ...] = ()


@dataclass(frozen=True)
class AoiRow:
    ref_des: str
    part_name: str
    x_mil: float
    y_mil: float
    angle_deg: int


@dataclass(frozen=True)
class AoiSideDocument:
    side: BoardSide
    rows: tuple[AoiRow, ...]
    # Positions only; FID<n> numbering is applied at render time.
    fiducials: tuple[tuple[float, float], ...]
    frame_status: FrameStatus
    fiducial_status: FiducialStatus
    flags: tuple[AoiFlag, ...] = ()
    frame: Optional[ProgramFrame] = None

    @property
    def fiducial_count(self) -> int:
        return len(self.fiducials)

    @property
    def is_complete(self) -> bool:
        return (self.frame_status is FrameStatus.PROGRAM_ORIGIN
                and self.fiducial_status is FiducialStatus.PROGRAM_MARKS)


@dataclass(frozen=True)
class AoiDifference:
    ref_des: str
    kind: str
    expected: str
    existing: str


@dataclass(frozen=True)
class AoiComparison:
    side: BoardSide
    existing: Path
    missing_in_existing: tuple[str, ...] = ()
    extra_in_existing: tuple[str, ...] = ()
    differences: tuple[AoiDifference, ...] = field(default_factory=tuple)

    @property
    def is_identical(self) -> bool:
        return not (self.missing_in_existing or self.extra_in_existing or self.differences)
