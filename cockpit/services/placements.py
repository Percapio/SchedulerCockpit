"""Placements read back for the canvas, the sides and the report (Phase 57 §10, §11).

UI thread, stored rows only: nothing here reads a file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.repositories.placements import (
    BoardPlacementRepository,
    PageRegistrationRepository,
    PlacementRejectionRepository,
)
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.types import (
    AuditBomComponent,
    PdfComponentCoord,
    PlacementRejection,
    SourceFile,
    SourceFileCategory,
)
from cockpit.placements.bmb_file import BmbPlacement
from cockpit.placements.geometry import LABEL, PLACEMENT, PartHighlight, outline_on_page
from cockpit.placements.registration import PageOutcome, Registered, map_to_page
from cockpit.placements.wording import reason_text
from cockpit.utils.sorting import natural_sort_key

TOP = "T"
BOTTOM = "B"

# SideSource
SIDE_FROM_PLACEMENT = "PLACEMENT"
SIDE_FROM_REGISTERED_PAGE = "REGISTERED_PAGE"
SIDE_FROM_LABEL_PAGE = "LABEL_PAGE"

LABEL_PAGE_SIDES = {0: TOP, 1: BOTTOM}


@dataclass(frozen=True)
class PartLocations:
    highlights: tuple[PartHighlight, ...]
    not_located: frozenset[str]
    has_placements: bool
    placements_unused_reason: Optional[str]


@dataclass(frozen=True)
class DesignatorSide:
    side: str     # TOP or BOTTOM
    source: str   # SIDE_FROM_*


@dataclass(frozen=True)
class PlacementsFile:
    name: str
    share_size: Optional[int]
    share_modified_ns: Optional[int]
    read_at: datetime


@dataclass(frozen=True)
class PlacementsReport:
    """What *Placements report…* shows, built from stored rows (§10.5)."""

    file: Optional[PlacementsFile]
    bom_designators: int
    placed: int
    not_placed: tuple[str, ...]
    marked_unstuffed: tuple[str, ...]
    pages: tuple[PageOutcome, ...]
    rejections: tuple[PlacementRejection, ...]
    newest_file_not_used: bool

    @property
    def nothing_recorded(self) -> bool:
        return self.file is None and not self.rejections


@dataclass(frozen=True)
class _AuditRows:
    components: tuple[AuditBomComponent, ...]
    drawing: Optional[SourceFile]
    coords: tuple[PdfComponentCoord, ...]
    placements_row: Optional[SourceFile]
    placements: tuple[BmbPlacement, ...]
    pages: tuple[PageOutcome, ...]


def _label_outline(coord: PdfComponentCoord) -> tuple[tuple[float, float], ...]:
    return ((coord.x1, coord.y1), (coord.x2, coord.y1), (coord.x2, coord.y2), (coord.x1, coord.y2))


class PlacementQueryService:
    def __init__(
        self,
        source_files: SourceFileRepository,
        bom_components: AuditBomComponentRepository,
        pdf_coords: PdfComponentCoordRepository,
        board_placements: BoardPlacementRepository,
        page_registrations: PageRegistrationRepository,
        placement_rejections: PlacementRejectionRepository,
    ) -> None:
        self.source_files = source_files
        self.bom_components = bom_components
        self.pdf_coords = pdf_coords
        self.board_placements = board_placements
        self.page_registrations = page_registrations
        self.placement_rejections = placement_rejections

    @classmethod
    def over(
        cls,
        conn: sqlite3.Connection,
        source_files: SourceFileRepository,
        bom_components: AuditBomComponentRepository,
        pdf_coords: PdfComponentCoordRepository,
    ) -> "PlacementQueryService":
        """The service over the repositories a caller already holds, and the placements tables of conn."""
        return cls(
            source_files, bom_components, pdf_coords, BoardPlacementRepository(conn),
            PageRegistrationRepository(conn), PlacementRejectionRepository(conn),
        )

    def _rows(self, audit_id: int) -> _AuditRows:
        bom = self.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        drawing = self.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
        placements_row = self.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.PLACEMENTS)
        pages: tuple[PageOutcome, ...] = ()
        if drawing is not None and placements_row is not None:
            pages = tuple(self.page_registrations.list_for(drawing.id, placements_row.id))
        return _AuditRows(
            components=tuple(self.bom_components.list_for_source_file(bom.id)) if bom is not None else (),
            drawing=drawing,
            coords=tuple(self.pdf_coords.list_for_source_file(drawing.id)) if drawing is not None else (),
            placements_row=placements_row,
            placements=(
                tuple(self.board_placements.list_for_file(placements_row.id)) if placements_row is not None else ()
            ),
            pages=pages,
        )

    def _unused_reason(self, audit_id: int) -> Optional[str]:
        rejections = self.placement_rejections.list_for_audit(audit_id)
        return reason_text(rejections[0].reason) if rejections else None

    def locate_parts(self, audit_id: int, designators: Optional[Iterable[str]] = None) -> PartLocations:
        """Resolves designators to page geometry, preferring placements over labels (§10.1).

        pre:  UI thread; designators, when given, come from the audit's current BOM or its X-ray
              locations; absent, they are every designator of the audit's BOM components
        post: every designator is in not_located or has at least one highlight, never both; it gets
              PLACEMENT highlights, one per registered page of its side, iff it has an admitted
              placement and such a page exists; otherwise one LABEL highlight iff the drawing has a
              label box for it, the lowest page's; has_placements iff the Placements row holds a
              placement; placements_unused_reason is the latest recorded rejection's reason iff
              has_placements is false
        raises: PersistenceUnavailable
        """
        rows = self._rows(audit_id)
        wanted = (
            set(designators) if designators is not None else {c.ref_des for c in rows.components}
        )
        registered_by_side: dict[str, list[Registered]] = {}
        for page in rows.pages:
            if isinstance(page, Registered):
                registered_by_side.setdefault(page.registration.side, []).append(page)
        placements = {p.designator: p for p in rows.placements}
        labels: dict[str, PdfComponentCoord] = {}
        for coord in sorted(rows.coords, key=lambda c: c.page_index):
            labels.setdefault(coord.ref_des, coord)

        highlights: list[PartHighlight] = []
        not_located: set[str] = set()
        for designator in sorted(wanted, key=natural_sort_key):
            placement = placements.get(designator)
            pages = registered_by_side.get(placement.side, []) if placement is not None else []
            if pages:
                for page in pages:
                    transform = page.registration.board_to_page
                    highlights.append(PartHighlight(
                        designator, page.registration.page_index, outline_on_page(placement, transform),
                        PLACEMENT, map_to_page(placement.placement.x_mils, placement.placement.y_mils, transform),
                    ))
            elif designator in labels:
                coord = labels[designator]
                highlights.append(PartHighlight(
                    designator, coord.page_index, _label_outline(coord), LABEL,
                    ((coord.x1 + coord.x2) / 2.0, (coord.y1 + coord.y2) / 2.0),
                ))
            else:
                not_located.add(designator)

        has_placements = bool(rows.placements)
        return PartLocations(
            highlights=tuple(highlights),
            not_located=frozenset(not_located),
            has_placements=has_placements,
            placements_unused_reason=None if has_placements else self._unused_reason(audit_id),
        )

    def board_sides(self, audit_id: int) -> dict[str, DesignatorSide]:
        """Which side of the board each designator is on (§11).

        post: an admitted placement's side wins; otherwise a label on a registered page takes that
              page's side; otherwise a label on page 0 means top and on page 1 bottom; with labels on
              several pages, the lowest page's under the same precedence; anything else is absent
        raises: PersistenceUnavailable
        """
        rows = self._rows(audit_id)
        registered_side = {
            page.registration.page_index: page.registration.side
            for page in rows.pages if isinstance(page, Registered)
        }
        sides: dict[str, DesignatorSide] = {
            p.designator: DesignatorSide(p.side, SIDE_FROM_PLACEMENT) for p in rows.placements
        }
        best: dict[str, tuple[int, int, DesignatorSide]] = {}
        for coord in rows.coords:
            if coord.ref_des in sides:
                continue
            if coord.page_index in registered_side:
                candidate = (0, coord.page_index,
                             DesignatorSide(registered_side[coord.page_index], SIDE_FROM_REGISTERED_PAGE))
            elif coord.page_index in LABEL_PAGE_SIDES:
                candidate = (1, coord.page_index,
                             DesignatorSide(LABEL_PAGE_SIDES[coord.page_index], SIDE_FROM_LABEL_PAGE))
            else:
                continue
            current = best.get(coord.ref_des)
            if current is None or candidate[:2] < current[:2]:
                best[coord.ref_des] = candidate
        sides.update({designator: entry[2] for designator, entry in best.items()})
        return sides

    def smt_side_count(self, audit_id: int) -> int:
        """The sides holding at least one SMT designator of the BOM, or 1 when none does (§11)."""
        rows = self._rows(audit_id)
        sides = self.board_sides(audit_id)
        smt = {c.ref_des for c in rows.components if c.mount_type == "S"}
        held = {sides[d].side for d in smt if d in sides}
        return max(1, len(held))

    def report(self, audit_id: int) -> PlacementsReport:
        """*Placements report…*'s content, from stored rows only (§10.5)."""
        rows = self._rows(audit_id)
        rejections = tuple(self.placement_rejections.list_for_audit(audit_id))
        bom = sorted({c.ref_des for c in rows.components}, key=natural_sort_key)
        placed = {p.designator for p in rows.placements}
        file: Optional[PlacementsFile] = None
        if rows.placements_row is not None:
            row = rows.placements_row
            file = PlacementsFile(row.original_filename, row.source_size, row.source_modified_ns, row.ingested_at)
        newest_not_used = bool(rejections) and (file is None or rejections[0].seen_at > file.read_at)
        return PlacementsReport(
            file=file,
            bom_designators=len(bom),
            placed=len([d for d in bom if d in placed]),
            not_placed=tuple(d for d in bom if d not in placed) if file is not None else (),
            marked_unstuffed=tuple(
                sorted((p.designator for p in rows.placements if not p.is_stuffed), key=natural_sort_key)
            ),
            pages=rows.pages,
            rejections=rejections,
            newest_file_not_used=newest_not_used,
        )
