"""Service for layout and rendering queries."""

from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.types import SourceFileCategory
from cockpit.layout.renderer import PdfRenderer
from cockpit.services.views import LayoutContext, ResolvedSelection, ResolutionKind, HighlightCoord


class LayoutQueryService:
    def __init__(
        self,
        source_file_repo: SourceFileRepository,
        pdf_renderer: PdfRenderer,
        bom_component_repo: AuditBomComponentRepository,
        pdf_coord_repo: PdfComponentCoordRepository,
    ) -> None:
        self.source_file_repo = source_file_repo
        self.pdf_renderer = pdf_renderer
        self.bom_component_repo = bom_component_repo
        self.pdf_coord_repo = pdf_coord_repo

    def load_for_audit(self, audit_id: int) -> LayoutContext:
        """Return the canvas's context for one audit."""
        source_file = self.source_file_repo.find_by_audit_and_category(
            audit_id, SourceFileCategory.PDF
        )

        if source_file is None:
            return LayoutContext(
                audit_id=audit_id,
                pdf_source_file_id=None,
                pdf_path=None,
                page_count=0,
                page_dimensions=()
            )

        pdf_path = source_file.local_storage_path
        dimensions = self.pdf_renderer.get_page_dimensions(pdf_path)

        return LayoutContext(
            audit_id=audit_id,
            pdf_source_file_id=source_file.id,
            pdf_path=pdf_path,
            page_count=len(dimensions),
            page_dimensions=dimensions
        )

    def resolve_selection(self, audit_id: int, mpn: str) -> ResolvedSelection:
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return ResolvedSelection(
                kind=ResolutionKind.UNKNOWN_MPN,
                mpn=mpn,
                ref_des_list=(),
                coords=()
            )

        bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id)
        ref_des_list = tuple(
            c.ref_des for c in bom_components if c.component_mpn == mpn
        )

        if not ref_des_list:
            return ResolvedSelection(
                kind=ResolutionKind.UNKNOWN_MPN,
                mpn=mpn,
                ref_des_list=(),
                coords=()
            )

        pdf_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
        if pdf_sf is None:
            return ResolvedSelection(
                kind=ResolutionKind.NO_PDF,
                mpn=mpn,
                ref_des_list=ref_des_list,
                coords=()
            )

        if len(ref_des_list) > 1:
            return ResolvedSelection(
                kind=ResolutionKind.MULTI_REFDES,
                mpn=mpn,
                ref_des_list=ref_des_list,
                coords=() # MULTI_REFDES doesn't need to return all coords, it suppresses bbox
            )

        # Single RefDes case
        ref_des = ref_des_list[0]
        pdf_coords = self.pdf_coord_repo.list_for_source_file(pdf_sf.id)
        
        filtered_coords = tuple(
            HighlightCoord(
                ref_des=c.ref_des,
                page_index=c.page_index,
                x1=c.x1,
                y1=c.y1,
                x2=c.x2,
                y2=c.y2
            )
            for c in pdf_coords if c.ref_des == ref_des
        )

        if not filtered_coords:
            return ResolvedSelection(
                kind=ResolutionKind.ABSENT_FROM_PDF,
                mpn=mpn,
                ref_des_list=ref_des_list,
                coords=()
            )

        return ResolvedSelection(
            kind=ResolutionKind.SINGLE_REFDES,
            mpn=mpn,
            ref_des_list=ref_des_list,
            coords=filtered_coords
        )
