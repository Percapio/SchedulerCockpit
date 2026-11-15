"""How a placements reason reads to people (Phase 57 §10.4).

One wording for the update summaries, the check after ingest, the hint and the report.
"""

from __future__ import annotations

import math
from typing import Any

from cockpit.placements.admission import BomDisagrees
from cockpit.placements.registration import (
    MAX_CONTROL_POINTS_PER_SIDE,
    MIN_INLIERS,
    LabelsDisagree,
    LabelsOffPart,
    MixedSides,
    NoFit,
    TooFewLabels,
    TooManyLabels,
)
from cockpit.placements.bmb_file import (
    ContentAfterEnd,
    DecodeCheckFailed,
    DecoderUnavailable,
    DuplicateDesignator,
    ExceedsLimit,
    RecordUnreadable,
    SectionMissing,
    SectionOutOfOrder,
    SectionRepeated,
    SUPPORTED_VERSION,
    UnknownMarker,
    UnrecognisedReason,
    UnsupportedVersion,
)

LIMIT_LABELS: dict[str, str] = {
    "max_share_bytes": "file size",
    "max_kept_bytes": "kept sections",
    "max_records": "placement records",
    "max_decals": "footprints",
    "max_decal_vertices": "footprint points",
    "max_attachment_sections": "attachments",
}

LAYOUT_NOT_RECOGNISED = "the .bmb file's layout isn't one Cockpit recognises"


def coverage_text(coverage: float) -> str:
    """A coverage as a whole percentage, rounded down, so 89.6% never reads as the 90% it missed."""
    return f"{math.floor(coverage * 100 + 1e-9)}%"


def reason_text(reason: Any) -> str:
    """The words for a BmbRejected or AdmissionRefused variant, or a kind a newer Cockpit wrote."""
    if isinstance(reason, UnsupportedVersion):
        return f"BOM Builder file version {reason.found}; Cockpit reads {SUPPORTED_VERSION}"
    if isinstance(reason, (UnknownMarker, SectionMissing, SectionRepeated, SectionOutOfOrder, ContentAfterEnd)):
        return LAYOUT_NOT_RECOGNISED
    if isinstance(reason, DecodeCheckFailed):
        return "the .bmb file didn't decode"
    if isinstance(reason, RecordUnreadable):
        return "a placement record couldn't be read"
    if isinstance(reason, DuplicateDesignator):
        return "the .bmb file lists a designator twice"
    if isinstance(reason, ExceedsLimit):
        return f"the .bmb file is larger than Cockpit reads ({LIMIT_LABELS.get(reason.limit, reason.limit)})"
    if isinstance(reason, BomDisagrees):
        return f"only {coverage_text(reason.coverage)} of the BOM's designators are placed in the .bmb file"
    if isinstance(reason, DecoderUnavailable):
        return "this build of Cockpit can't read BOM Builder files"
    if isinstance(reason, UnrecognisedReason):
        return reason.kind
    return type(reason).__name__


def refusal_text(refusal: Any) -> str:
    """The words for why a drawing page didn't register, for the report (§10.5)."""
    if isinstance(refusal, TooFewLabels):
        return f"{refusal.found} labels fit; {MIN_INLIERS} are needed"
    if isinstance(refusal, TooManyLabels):
        return f"{refusal.found:,} labels on one side; Cockpit fits up to {MAX_CONTROL_POINTS_PER_SIDE:,}"
    if isinstance(refusal, NoFit):
        return "no two labels gave a fit"
    if isinstance(refusal, LabelsDisagree):
        return f"only {coverage_text(refusal.inlier_ratio)} of the labels agree"
    if isinstance(refusal, LabelsOffPart):
        return f"labels sit {refusal.residual_p90_mils:.1f} mil from their parts (p90)"
    if isinstance(refusal, MixedSides):
        return f"labels of both sides fit ({refusal.top_inliers} top, {refusal.bottom_inliers} bottom)"
    return type(refusal).__name__
