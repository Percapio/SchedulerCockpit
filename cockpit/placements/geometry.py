"""Outlines on the page, and the part under a click (Phase 57 §6.3, §10.1)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from cockpit.placements.registration import BoardToPage, map_to_page
from cockpit.utils.sorting import natural_sort_key

PLACEMENT_WITHOUT_BODY_MILS = 25.0
CLICK_TOLERANCE_PT = 2.0
ZERO_AREA_SQUARE_PT = 2.0

PLACEMENT = "PLACEMENT"
LABEL = "LABEL"

Point = tuple[float, float]


@dataclass(frozen=True)
class PartHighlight:
    designator: str
    page_index: int
    outline: tuple[Point, ...]
    source: str          # PLACEMENT or LABEL
    point: Point         # the placement point mapped to the page, or a label box's centre


def outline_on_page(placement, transform: BoardToPage) -> tuple[Point, ...]:
    """Maps an admitted placement's footprint outline onto a registered page (rule B for BOTTOM).

    post: the corners are the body rectangle's, relative to the part; TOP rotates them by the
          placement's rotation, BOTTOM negates x and rotates by its negative; each is offset by the
          placement point and mapped; without a body, a PLACEMENT_WITHOUT_BODY_MILS square centred on
          the point
    """
    triplet = placement.placement
    x0, y0 = float(triplet.x_mils), float(triplet.y_mils)
    body = placement.body
    if body is None:
        half = PLACEMENT_WITHOUT_BODY_MILS / 2.0
        corners = [(x0 - half, y0 - half), (x0 + half, y0 - half), (x0 + half, y0 + half), (x0 - half, y0 + half)]
    else:
        hw, hh = body.width_mils / 2.0, body.height_mils / 2.0
        local = [
            (body.centre_x_mils - hw, body.centre_y_mils - hh),
            (body.centre_x_mils + hw, body.centre_y_mils - hh),
            (body.centre_x_mils + hw, body.centre_y_mils + hh),
            (body.centre_x_mils - hw, body.centre_y_mils + hh),
        ]
        angle = math.radians(triplet.rotation_ccw_degrees)
        if placement.side == "B":
            local = [(-x, y) for x, y in local]
            angle = -angle
        cos, sin = math.cos(angle), math.sin(angle)
        corners = [(x0 + x * cos - y * sin, y0 + x * sin + y * cos) for x, y in local]
    return tuple(map_to_page(x, y, transform) for x, y in corners)


def polygon_area(points: Sequence[Point]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, list(points[1:]) + [points[0]]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _on_segment(x: float, y: float, a: Point, b: Point) -> bool:
    cross = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
    if abs(cross) > 1e-9:
        return False
    return min(a[0], b[0]) - 1e-9 <= x <= max(a[0], b[0]) + 1e-9 and min(a[1], b[1]) - 1e-9 <= y <= max(a[1], b[1]) + 1e-9


def contains(points: Sequence[Point], x: float, y: float) -> bool:
    """Whether (x, y) lies inside the polygon or on its boundary."""
    inside = False
    count = len(points)
    for index in range(count):
        a, b = points[index], points[(index + 1) % count]
        if _on_segment(x, y, a, b):
            return True
        if (a[1] > y) != (b[1] > y):
            crossing = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x < crossing:
                inside = not inside
    return inside


def _box(points: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def part_at(page_index: int, x: float, y: float, shown: Iterable[PartHighlight]) -> Optional[str]:
    """Finds the part under a click on the displayed page (Phase 57 §10.1).

    post: the smallest-area PLACEMENT outline containing the point, boundary included and with no
          tolerance, wins, a zero-area outline counting as a 2 pt square centred on its mapped
          placement point; otherwise the LABEL box of smallest raw area within 2 pt; otherwise none;
          ties go to the lower designator in natural order
    """
    placements: list[tuple[float, list, str]] = []
    labels: list[tuple[float, list, str]] = []
    for highlight in shown:
        if highlight.page_index != page_index:
            continue
        if highlight.source == PLACEMENT:
            area = polygon_area(highlight.outline)
            if area == 0:
                half = ZERO_AREA_SQUARE_PT / 2.0
                px, py = highlight.point
                hit = abs(x - px) <= half and abs(y - py) <= half
            else:
                hit = contains(highlight.outline, x, y)
            if hit:
                placements.append((area, natural_sort_key(highlight.designator), highlight.designator))
        else:
            x0, y0, x1, y1 = _box(highlight.outline)
            if (x0 - CLICK_TOLERANCE_PT) <= x <= (x1 + CLICK_TOLERANCE_PT) and (y0 - CLICK_TOLERANCE_PT) <= y <= (y1 + CLICK_TOLERANCE_PT):
                labels.append(((x1 - x0) * (y1 - y0), natural_sort_key(highlight.designator), highlight.designator))
    for hits in (placements, labels):
        if hits:
            return min(hits, key=lambda h: (h[0], h[1]))[2]
    return None
