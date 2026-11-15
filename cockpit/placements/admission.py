"""Admission: which of a .bmb's placements an audit may use (Phase 57 §6.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from cockpit.placements.bmb_file import BmbPlacement, BmbProject

MIN_BOM_COVERAGE_PERCENT = 90


@dataclass(frozen=True)
class PlacementAdmission:
    admitted: Mapping[str, BmbPlacement]
    missing_from_file: frozenset[str]
    unplaced_in_file: frozenset[str]
    stuffing_disagrees: frozenset[str]
    outside_bom: int
    coverage: float


@dataclass(frozen=True)
class BomDisagrees:
    coverage: float


ADMISSION_TYPES: dict[str, type] = {"BomDisagrees": BomDisagrees}


def admit_placements(
    project: BmbProject,
    bom_designators: Iterable[str],
) -> PlacementAdmission | BomDisagrees:
    """Decides which placements an audit may use.

    pre:  project comes from read_bmb_project; bom_designators are the audit's non-DNI designators
    post: BomDisagrees(0) iff bom_designators is empty; BomDisagrees iff fewer than 90% of them have
          a placed record; otherwise every BOM designator with a placed record is admitted, those the
          file marks unstuffed also listed in stuffing_disagrees; BOM designators with no record, or
          only an unplaced one, are listed and fall back to labels; records outside the BOM are
          counted and ignored, whatever their text
    """
    bom = set(bom_designators)
    if not bom:
        return BomDisagrees(0.0)
    placed = {p.designator: p for p in project.placements}
    admitted = {p.designator: p for p in project.placements if p.designator in bom}
    coverage = len(admitted) / len(bom)
    if len(admitted) * 100 < MIN_BOM_COVERAGE_PERCENT * len(bom):
        return BomDisagrees(coverage)
    unplaced = frozenset(d for d in bom if d not in placed and d in project.unplaced)
    missing = frozenset(bom - set(admitted) - unplaced)
    outside = sum(1 for d in placed if d not in bom) + sum(1 for d in project.unplaced if d not in bom)
    return PlacementAdmission(
        admitted=admitted,
        missing_from_file=missing,
        unplaced_in_file=unplaced,
        stuffing_disagrees=frozenset(d for d, p in admitted.items() if not p.is_stuffed),
        outside_bom=outside,
        coverage=coverage,
    )
