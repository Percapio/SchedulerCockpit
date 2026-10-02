"""The program frame: origin offset and mirroring (Phase 52 section 3.4).

AOI_x = CAD_x + O_x, AOI_y = CAD_y + O_y (mils). The program stores
P_x = s * AOI_x * 254 and P_y = AOI_y * 254, with s = +1 (identity) or -1
(mirror-X, bottom-side programs). So for each matched designator d:
O_x = s * P_x(d) / 254 - CAD_x(d) and O_y = P_y(d) / 254 - CAD_y(d).
"""

import math
import statistics
from typing import Sequence

from .errors import FrameFitError
from .juki_program import UNITS_PER_MIL
from .types import BoardSide, CadPlacement, DecodedProgram, FrameTolerance, ProgramFrame


def side_label(side: BoardSide) -> str:
    return "Top" if side is BoardSide.TOP else "Bottom"


def _fit(side: BoardSide, cad_by_ref: dict[str, CadPlacement], program: DecodedProgram,
         tolerance: FrameTolerance) -> ProgramFrame:
    pairs = [(cad_by_ref[ref], placement) for ref, placement in program.placements.items()
             if ref in cad_by_ref]
    if len(pairs) < tolerance.min_matches:
        raise FrameFitError(
            side_label(side),
            f"{program.file.path.name} shares only {len(pairs)} designator(s) with the XY file "
            f"(need {tolerance.min_matches})",
            matched=len(pairs),
        )

    best = None
    for sign in (1, -1):
        dx = [sign * p.x_units / UNITS_PER_MIL - c.x_mil for c, p in pairs]
        dy = [p.y_units / UNITS_PER_MIL - c.y_mil for c, p in pairs]
        offset_x = round(statistics.median(dx), 2)
        offset_y = round(statistics.median(dy), 2)
        residuals = [math.hypot(a - offset_x, b - offset_y) for a, b in zip(dx, dy)]
        inliers = sum(1 for r in residuals if r <= tolerance.max_residual_mil)
        candidate = (inliers, -statistics.median(residuals), sign, offset_x, offset_y, residuals)
        if best is None or candidate[:2] > best[:2]:
            best = candidate

    inliers, _neg_median, sign, offset_x, offset_y, residuals = best
    if inliers < tolerance.min_matches or inliers / len(pairs) < tolerance.min_inlier_ratio:
        raise FrameFitError(
            side_label(side),
            f"{program.file.path.name}: only {inliers} of {len(pairs)} placements line up with the "
            f"XY file within {tolerance.max_residual_mil:g} mil (rotated or panelized program?)",
            matched=len(pairs),
            max_residual_mil=max(residuals),
        )
    outliers = tuple(sorted(
        c.ref_des for (c, _p), r in zip(pairs, residuals) if r > tolerance.max_residual_mil
    ))
    inlier_residuals = [r for r in residuals if r <= tolerance.max_residual_mil]
    return ProgramFrame(
        offset_x_mil=offset_x,
        offset_y_mil=offset_y,
        mirrored_x=sign == -1,
        matched=len(pairs),
        max_residual_mil=max(inlier_residuals),
        outliers=outliers,
    )


def derive_frame(
    side: BoardSide,
    cad: Sequence[CadPlacement],
    programs: Sequence[DecodedProgram],
    tolerance: FrameTolerance | None = None,
) -> ProgramFrame:
    """Derives the AOI frame for one side from its program(s).

    pre:  cad holds one side's placements; programs belong to the same side
    post: for the chosen sign, at least tolerance.min_matches placements and at
          least tolerance.min_inlier_ratio of them lie within
          tolerance.max_residual_mil; the rest are reported as outliers; split
          programs (T1/T2) yield the same frame within tolerance
    raises: FrameFitError
    """
    tolerance = tolerance or FrameTolerance()
    if not programs:
        raise FrameFitError(side_label(side), "no program for this side")
    cad_by_ref = {p.ref_des: p for p in cad}
    frames = [_fit(side, cad_by_ref, program, tolerance) for program in programs]

    first = frames[0]
    for program, frame in zip(programs[1:], frames[1:]):
        if (frame.mirrored_x != first.mirrored_x
                or abs(frame.offset_x_mil - first.offset_x_mil) > tolerance.max_residual_mil
                or abs(frame.offset_y_mil - first.offset_y_mil) > tolerance.max_residual_mil):
            raise FrameFitError(
                side_label(side),
                f"{programs[0].file.path.name} and {program.file.path.name} use different origins",
                matched=sum(f.matched for f in frames),
            )
    return ProgramFrame(
        offset_x_mil=first.offset_x_mil,
        offset_y_mil=first.offset_y_mil,
        mirrored_x=first.mirrored_x,
        matched=sum(f.matched for f in frames),
        max_residual_mil=max(f.max_residual_mil for f in frames),
        outliers=tuple(sorted({ref for f in frames for ref in f.outliers})),
    )


def marks_in_aoi_frame(marks, frame: ProgramFrame) -> tuple[tuple[float, float], ...]:
    """Program fiducial marks as AOI-frame mils, rounded to 0.01 mil.

    post: AOI_x = s * M_x / 254, AOI_y = M_y / 254
    """
    sign = -1 if frame.mirrored_x else 1
    return tuple(
        (round(sign * mark.x_units / UNITS_PER_MIL, 2), round(mark.y_units / UNITS_PER_MIL, 2))
        for mark in marks
    )
