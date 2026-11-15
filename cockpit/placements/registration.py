"""Registration: one board-to-page similarity per drawing page, fitted exactly as the spike fitted.

Phase 57 §6.2. The spike's gate held with no page to spare (805 of 894), and once its data is gone no
other fit can be checked against the year, so every step below is the spike's, in its order.
"""

from __future__ import annotations

import array
import math
import random
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

from cockpit.utils.sorting import natural_sort_key

INLIER_MILS = 25.0
MIN_INLIERS = 6
MIN_INLIER_RATIO_PERCENT = 90
MAX_RESIDUAL_P90_MILS = 15.0
MAX_CANDIDATES_PER_PAGE = 2000
MIN_PAIR_SEPARATION_MILS = 50.0
CANDIDATE_SEED = 57
MAX_CONTROL_POINTS_PER_SIDE = 7000
STOP_CHECK_EVERY = 256


@dataclass(frozen=True)
class DrawingLabel:
    designator: str
    page_index: int
    box: tuple[float, float, float, float]  # x0, y0, x1, y1, page points
    from_split: bool = False


@dataclass(frozen=True)
class BoardToPage:
    scale_cos: float
    scale_sin: float
    offset_x_pt: float
    offset_y_pt: float
    mirrored: bool


@dataclass(frozen=True)
class PageRegistration:
    page_index: int
    side: str  # 'T' or 'B'
    board_to_page: BoardToPage
    inliers: int
    matched_labels: int
    residual_p90_mils: float
    candidates_tried: int


@dataclass(frozen=True)
class TooFewLabels:
    found: int


@dataclass(frozen=True)
class TooManyLabels:
    found: int


@dataclass(frozen=True)
class NoFit:
    pass


@dataclass(frozen=True)
class LabelsDisagree:
    inlier_ratio: float


@dataclass(frozen=True)
class LabelsOffPart:
    residual_p90_mils: float


@dataclass(frozen=True)
class MixedSides:
    top_inliers: int
    bottom_inliers: int


RegistrationRefusal = Union[TooFewLabels, TooManyLabels, NoFit, LabelsDisagree, LabelsOffPart, MixedSides]


@dataclass(frozen=True)
class FitSummary:
    """The best fit of a refused page: kept for the report and the fixture, never for drawing."""
    board_to_page: BoardToPage
    inliers: int
    residual_p90_mils: Optional[float]
    candidates_tried: int


@dataclass(frozen=True)
class Registered:
    registration: PageRegistration


@dataclass(frozen=True)
class Refused:
    page_index: int
    side: Optional[str]
    matched_labels: int
    reasons: tuple[RegistrationRefusal, ...]
    best: Optional[FitSummary] = None


PageOutcome = Union[Registered, Refused]


@dataclass(frozen=True)
class RegistrationStopped:
    pass


def map_to_page(x_mils: float, y_mils: float, transform: BoardToPage) -> tuple[float, float]:
    """Maps a board point onto a page: with x' = -x when mirrored, a similarity."""
    xp = -x_mils if transform.mirrored else x_mils
    return (
        transform.scale_cos * xp - transform.scale_sin * y_mils + transform.offset_x_pt,
        transform.scale_sin * xp + transform.scale_cos * y_mils + transform.offset_y_pt,
    )


# ---------- one side's fit ----------

@dataclass(frozen=True)
class _Point:
    designator: str
    board: complex
    page: complex


@dataclass(frozen=True)
class _SideFit:
    transform: BoardToPage
    inliers: int
    residual_p90_mils: float
    candidates_tried: int


def _offset(i: int, n: int) -> int:
    return i * n - i * (i + 1) // 2


def _decode_pair(k: int, n: int) -> tuple[int, int]:
    """Pair number k, in the listing by i and then j (i < j), as its (i, j)."""
    i = int((2 * n - 1 - math.sqrt(max((2 * n - 1) ** 2 - 8 * k, 0))) / 2)
    i = max(0, min(i, n - 2))
    while i + 1 <= n - 2 and _offset(i + 1, n) <= k:
        i += 1
    while i > 0 and _offset(i, n) > k:
        i -= 1
    return i, k - _offset(i, n) + i + 1


def candidate_order(n: int) -> array.array:
    """Every pair number, shuffled once with Random(CANDIDATE_SEED): the spike's permutation.

    shuffle's swaps depend only on the sequence's length, so shuffling pair numbers applies the same
    permutation the spike applied to its list of pairs, at 4 bytes a pair.
    """
    order = array.array("I", range(n * (n - 1) // 2))
    random.Random(CANDIDATE_SEED).shuffle(order)
    return order


def _board(point: _Point, mirrored: bool) -> complex:
    return complex(-point.board.real, point.board.imag) if mirrored else point.board


def _as_transform(a: complex, t: complex, mirrored: bool) -> BoardToPage:
    return BoardToPage(a.real, a.imag, t.real, t.imag, mirrored)


def _residuals(points: Sequence[_Point], a: complex, t: complex, mirrored: bool) -> list[float]:
    scale = abs(a)
    return [abs(a * _board(p, mirrored) + t - p.page) / scale for p in points]


def _least_squares(points: Sequence[_Point], mirrored: bool) -> Optional[tuple[complex, complex]]:
    if len(points) < 2:
        return None
    boards = [_board(p, mirrored) for p in points]
    pages = [p.page for p in points]
    board_centre = sum(boards) / len(boards)
    page_centre = sum(pages) / len(pages)
    denominator = sum(abs(b - board_centre) ** 2 for b in boards)
    if denominator == 0:
        return None
    a = sum((p - page_centre) * (b - board_centre).conjugate() for b, p in zip(boards, pages)) / denominator
    if a == 0:
        return None
    return a, page_centre - a * board_centre


def _nearest_rank_p90(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(math.ceil(0.9 * len(ordered)), 1) - 1]


def _fit_side(points: list[_Point], stop: Any) -> Optional[_SideFit] | RegistrationStopped:
    n = len(points)
    if n < 2:
        return None
    points = sorted(points, key=lambda p: natural_sort_key(p.designator))
    order = candidate_order(n)
    best: tuple[int, float, complex, complex, bool] | None = None
    tried = 0
    everyone = False
    for k in order:
        i, j = _decode_pair(k, n)
        first, second = points[i], points[j]
        if abs(first.board - second.board) < MIN_PAIR_SEPARATION_MILS:
            continue
        if first.page == second.page:
            continue
        tried += 1
        if stop is not None and tried % STOP_CHECK_EVERY == 0 and stop.is_set():
            return RegistrationStopped()
        for mirrored in (False, True):
            b1, b2 = _board(first, mirrored), _board(second, mirrored)
            a = (second.page - first.page) / (b2 - b1)
            t = first.page - a * b1
            residuals = _residuals(points, a, t, mirrored)
            inliers = sum(1 for r in residuals if r <= INLIER_MILS)
            median = statistics.median(residuals)
            if best is None or inliers > best[0] or (inliers == best[0] and median < best[1]):
                best = (inliers, median, a, t, mirrored)
            if inliers == n:
                everyone = True
                break
        if everyone or tried >= MAX_CANDIDATES_PER_PAGE:
            break
    del order
    if best is None:
        return None
    _inliers, _median, a, t, mirrored = best
    candidate_residuals = _residuals(points, a, t, mirrored)
    inlier_points = [p for p, r in zip(points, candidate_residuals) if r <= INLIER_MILS]
    refit = _least_squares(inlier_points, mirrored)
    if refit is not None:
        a, t = refit
    final = _residuals(points, a, t, mirrored)
    kept = [r for r in final if r <= INLIER_MILS]
    p90 = _nearest_rank_p90(kept) if kept else float("inf")
    return _SideFit(_as_transform(a, t, mirrored), len(kept), p90, tried)


def _passes(fit: _SideFit, matched: int) -> bool:
    return (
        fit.inliers >= MIN_INLIERS
        and fit.inliers * 100 >= MIN_INLIER_RATIO_PERCENT * matched
        and fit.residual_p90_mils <= MAX_RESIDUAL_P90_MILS
    )


# ---------- the pages ----------

def register_drawing_pages(
    admitted: Mapping[str, Any],
    labels: Sequence[DrawingLabel],
    stop: Any = None,
) -> list[PageOutcome] | RegistrationStopped:
    """Fits one board-to-page similarity per drawing page (Phase 57 §6.2).

    pre:  admitted maps admitted designators to their BmbPlacement; labels belong to the drawing
    post: a label is a control point iff it isn't from a split, its designator is admitted, and its
          placement isn't at the origin; one outcome per page holding a control point; see §6.2
    """
    by_page: dict[int, list[tuple[_Point, str]]] = {}
    for label in labels:
        if label.from_split:
            continue
        placement = admitted.get(label.designator)
        if placement is None:
            continue
        triplet = placement.placement
        if triplet.x_mils == 0 and triplet.y_mils == 0:
            continue
        x0, y0, x1, y1 = label.box
        point = _Point(label.designator, complex(triplet.x_mils, triplet.y_mils),
                       complex((x0 + x1) / 2.0, (y0 + y1) / 2.0))
        by_page.setdefault(label.page_index, []).append((point, placement.side))

    outcomes: list[PageOutcome] = []
    for page_index in sorted(by_page):
        if stop is not None and stop.is_set():
            return RegistrationStopped()
        entries = by_page[page_index]
        matched = len(entries)
        sides = {side for _point, side in entries}
        only_side = next(iter(sides)) if len(sides) == 1 else None
        if matched < MIN_INLIERS:
            outcomes.append(Refused(page_index, None, matched, (TooFewLabels(matched),)))
            continue
        top = [p for p, side in entries if side == "T"]
        bottom = [p for p, side in entries if side == "B"]
        if max(len(top), len(bottom)) > MAX_CONTROL_POINTS_PER_SIDE:
            outcomes.append(Refused(page_index, only_side, matched,
                                    (TooManyLabels(max(len(top), len(bottom))),)))
            continue
        top_fit = _fit_side(top, stop)
        if isinstance(top_fit, RegistrationStopped):
            return top_fit
        bottom_fit = _fit_side(bottom, stop)
        if isinstance(bottom_fit, RegistrationStopped):
            return bottom_fit
        if top_fit is None and bottom_fit is None:
            outcomes.append(Refused(page_index, only_side, matched, (NoFit(),)))
            continue
        if bottom_fit is None or (top_fit is not None and top_fit.inliers >= bottom_fit.inliers):
            side, fit = "T", top_fit
        else:
            side, fit = "B", bottom_fit
        tried = (top_fit.candidates_tried if top_fit else 0) + (bottom_fit.candidates_tried if bottom_fit else 0)
        reasons: list[RegistrationRefusal] = []
        if fit.inliers < MIN_INLIERS:
            reasons.append(TooFewLabels(fit.inliers))
        if fit.inliers * 100 < MIN_INLIER_RATIO_PERCENT * matched:
            reasons.append(LabelsDisagree(fit.inliers / matched))
        if fit.residual_p90_mils > MAX_RESIDUAL_P90_MILS:
            reasons.append(LabelsOffPart(fit.residual_p90_mils))
        if not reasons:
            outcomes.append(Registered(PageRegistration(
                page_index, side, fit.transform, fit.inliers, matched, fit.residual_p90_mils, tried)))
            continue
        if (top_fit is not None and bottom_fit is not None
                and _passes(top_fit, len(top)) and _passes(bottom_fit, len(bottom))):
            reasons.append(MixedSides(top_fit.inliers, bottom_fit.inliers))
        p90 = fit.residual_p90_mils if math.isfinite(fit.residual_p90_mils) else None
        outcomes.append(Refused(page_index, only_side, matched, tuple(reasons),
                                FitSummary(fit.transform, fit.inliers, p90, tried)))
    return outcomes
