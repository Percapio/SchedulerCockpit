"""Layout parser result dataclasses."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PdfComponentCoordinate:
    ref_des: str
    page_index: int
    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if self.x1 > self.x2:
            raise ValueError(f"x1 ({self.x1}) cannot be greater than x2 ({self.x2})")
        if self.y1 > self.y2:
            raise ValueError(f"y1 ({self.y1}) cannot be greater than y2 ({self.y2})")


@dataclass(frozen=True)
class PdfLayoutResult:
    coordinates: list[PdfComponentCoordinate]
    found_ref_des: set[str]
    page_count: int
