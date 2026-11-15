from cockpit.utils.sorting import natural_sort_key
"""Service for layout and rendering queries."""

from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.types import SourceFileCategory
from cockpit.layout.renderer import PdfRenderer
from cockpit.services.views import LayoutContext, ResolvedSelection, ResolutionKind, HighlightCoord, SelectionIntent, SelectionKind, RefDesLocation, PendingPdf
from dataclasses import dataclass

from cockpit.placements.geometry import PLACEMENT, PartHighlight
from cockpit.services.placements import PartLocations, PlacementQueryService


@dataclass(frozen=True)
class AuditBomRowView:
    find_number: str
    component_mpn: str
    description: str | None
    mount_type: str
    ref_des_list: tuple[str, ...]


class LayoutQueryService:
    def __init__(
        self,
        source_file_repo: SourceFileRepository,
        pdf_renderer: PdfRenderer,
        bom_component_repo: AuditBomComponentRepository,
        pdf_coord_repo: PdfComponentCoordRepository,
        placement_query: PlacementQueryService | None = None,
    ) -> None:
        self.source_file_repo = source_file_repo
        self.pdf_renderer = pdf_renderer
        self.bom_component_repo = bom_component_repo
        self.pdf_coord_repo = pdf_coord_repo
        if placement_query is None:
            placement_query = PlacementQueryService.over(
                source_file_repo.conn, source_file_repo, bom_component_repo, pdf_coord_repo,
            )
        self.placement_query = placement_query

    def resolve_pdf_ref(self, audit_id: int) -> PendingPdf | None:
        """Resolve the cheap, DB-derived PDF reference for an audit."""
        source_file = self.source_file_repo.find_by_audit_and_category(
            audit_id, SourceFileCategory.PDF
        )

        if source_file is None:
            return None

        return PendingPdf(
            source_file_id=source_file.id,
            path=source_file.local_storage_path
        )

    def resolve_secondary_pdf_ref(self, audit_id: int) -> PendingPdf | None:
        """Resolve the secondary PDF reference for an audit."""
        source_file = self.source_file_repo.find_by_audit_and_category(
            audit_id, SourceFileCategory.SECONDARY_PDF
        )

        if source_file is None:
            return None

        return PendingPdf(
            source_file_id=source_file.id,
            path=source_file.local_storage_path
        )

    def list_clickable_parts(self, audit_id: int) -> tuple[PartHighlight, ...]:
        """Phase 57 §10.2: what the canvas offers for clicks, every BOM designator's locations."""
        return self.placement_query.locate_parts(audit_id).highlights

    def locate_refdes(self, audit_id: int, ref_des: str) -> RefDesLocation | None:
        """
        Resolves a Ref_Des to the BOM line that owns it. Where a split job lists the
        designator on more than one line, the lowest Find# wins.

        INVARIANT -- shared with SelectionCoordinator.on_renderer_refdes_clicked:
          that caller re-resolves the THT branch itself, by taking the first row of
          ActiveAuditView.tht_rows carrying the designator. It agrees with this
          function ONLY because tht_rows is ordered by natural_sort_key(find_number)
          (checklist.py sort_key, see 4.2) and this function selects by the same key.
          Change either ordering and the two desynchronise silently: mount_type is
          read off one BOM line while the row that gets selected and scrolled to
          belongs to another. Neither path raises when they disagree.
        """
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return None
        bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id)
        matches = [c for c in bom_components if c.ref_des == ref_des]
        if not matches:
            return None
        if len(matches) > 1:
            import logging
            logging.getLogger(__name__).debug("ref_des %s shared across %d split lines; routing to lowest Find#", ref_des, len(matches))
        
        owning_line = min(matches, key=lambda c: natural_sort_key(c.find_number))
        return RefDesLocation(mpn=owning_line.component_mpn, mount_type=owning_line.mount_type)

    def list_bom_rows_for_audit(self, audit_id: int) -> tuple[AuditBomRowView, ...]:
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return ()

        bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id)
        
        # Group by MPN, filter SMT
        grouped = {}
        for c in bom_components:
            if c.mount_type != 'S':
                continue
            if c.component_mpn not in grouped:
                grouped[c.component_mpn] = {
                    "description": c.description,
                    "mount_type": c.mount_type,
                    "ref_des_list": [],
                    "find_number": c.find_number
                }
            grouped[c.component_mpn]["ref_des_list"].append(c.ref_des)
            
        # Build views, sorting by Find#
        views = []
        for mpn, data in sorted(grouped.items(), key=lambda item: natural_sort_key(item[1]["find_number"])):
            views.append(AuditBomRowView(
                find_number=data["find_number"],
                component_mpn=mpn,
                description=data["description"],
                mount_type=data["mount_type"],
                ref_des_list=tuple(sorted(data["ref_des_list"], key=natural_sort_key))
            ))
            
        return tuple(views)

    def resolve_selection(self, audit_id: int, intent: SelectionIntent) -> ResolvedSelection:
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        pdf_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)

        bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id) if bom_sf else []

        # SelectionKind.THT_MPN / MPN_CELL / BOM_MPN logic
        if intent.mpn is None:
            raise ValueError("intent.mpn must not be None for this kind")
        mpn = intent.mpn

        if not bom_sf:
            return ResolvedSelection(
                kind=ResolutionKind.UNKNOWN_MPN,
                mpn=mpn,
                mpn_set=None,
                ref_des_list=(),
                coords=()
            )

        ref_des_list = tuple(c.ref_des for c in bom_components if c.component_mpn == mpn)
        if not ref_des_list:
            return ResolvedSelection(
                kind=ResolutionKind.UNKNOWN_MPN,
                mpn=mpn,
                mpn_set=None,
                ref_des_list=(),
                coords=()
            )

        if not pdf_sf:
            return ResolvedSelection(
                kind=ResolutionKind.NO_PDF,
                mpn=mpn,
                mpn_set=None,
                ref_des_list=ref_des_list,
                coords=()
            )

        locations = self.placement_query.locate_parts(audit_id, set(ref_des_list))
        coords = tuple(highlight_coord(h) for h in locations.highlights)
        n = len(ref_des_list)
        # Phase 57 §10.2: k counts located designators, not boxes; a placement can show on several pages.
        k = len(set(ref_des_list) - locations.not_located)
        located = _placements_fields(locations)

        if intent.kind == SelectionKind.THT_MPN:
            if n == 1 and k >= 1:
                return ResolvedSelection(
                    kind=ResolutionKind.SINGLE_REFDES,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=coords,
                    **located,
                )
            elif n == 1 and k == 0:
                return ResolvedSelection(
                    kind=ResolutionKind.ABSENT_FROM_PDF,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=(),
                    **located,
                )
            elif n > 1:
                return ResolvedSelection(
                    kind=ResolutionKind.MULTI_REFDES,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=(),
                    **located,
                )

        elif intent.kind in (SelectionKind.MPN_CELL, SelectionKind.BOM_MPN):
            if k == 0:
                return ResolvedSelection(
                    kind=ResolutionKind.GROUP_ABSENT,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=(),
                    **located,
                )
            else:
                return ResolvedSelection(
                    kind=ResolutionKind.GROUP_REFDES,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=coords,
                    **located,
                )

        return ResolvedSelection(
            kind=ResolutionKind.UNKNOWN_MPN,
            mpn=mpn,
            mpn_set=None,
            ref_des_list=ref_des_list,
            coords=(),
            **located,
        )


def highlight_coord(highlight: PartHighlight) -> HighlightCoord:
    """A located part as the canvas draws it: an outline for a placement, the label box for a label."""
    xs = [x for x, _y in highlight.outline]
    ys = [y for _x, y in highlight.outline]
    return HighlightCoord(
        ref_des=highlight.designator, page_index=highlight.page_index,
        x1=min(xs), y1=min(ys), x2=max(xs), y2=max(ys),
        outline=highlight.outline if highlight.source == PLACEMENT else None,
        source=highlight.source, point=highlight.point,
    )


def _placements_fields(locations: PartLocations) -> dict:
    return {
        "not_located": locations.not_located,
        "has_placements": locations.has_placements,
        "placements_unused_reason": locations.placements_unused_reason,
    }
