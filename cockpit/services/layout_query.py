"""Service for layout and rendering queries."""

from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.types import SourceFileCategory
from cockpit.layout.renderer import PdfRenderer
from cockpit.services.views import LayoutContext, ResolvedSelection, ResolutionKind, HighlightCoord, SelectionIntent, SelectionKind
from dataclasses import dataclass


@dataclass(frozen=True)
class AuditBomRowView:
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

    def list_bom_rows_for_audit(self, audit_id: int) -> tuple[AuditBomRowView, ...]:
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return ()

        bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id)
        
        # Group by MPN
        grouped = {}
        for c in bom_components:
            if c.component_mpn not in grouped:
                grouped[c.component_mpn] = {
                    "description": c.description,
                    "mount_type": c.mount_type,
                    "ref_des_list": []
                }
            grouped[c.component_mpn]["ref_des_list"].append(c.ref_des)
            
        # Build views, sorting by MPN and then RefDes
        views = []
        for mpn in sorted(grouped.keys()):
            views.append(AuditBomRowView(
                component_mpn=mpn,
                description=grouped[mpn]["description"],
                mount_type=grouped[mpn]["mount_type"],
                ref_des_list=tuple(sorted(grouped[mpn]["ref_des_list"]))
            ))
            
        return tuple(views)

    def resolve_selection(self, audit_id: int, intent: SelectionIntent) -> ResolvedSelection:
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        pdf_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)

        def _get_pdf_coords(ref_des_set):
            if pdf_sf is None:
                return ()
            pdf_coords = self.pdf_coord_repo.list_for_source_file(pdf_sf.id)
            return tuple(
                HighlightCoord(c.ref_des, c.page_index, c.x1, c.y1, c.x2, c.y2)
                for c in pdf_coords if c.ref_des in ref_des_set
            )

        bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id) if bom_sf else []

        if intent.kind == SelectionKind.BOM_REFDES:
            ref_des = intent.ref_des
            # Find MPN best effort
            matches = [c.component_mpn for c in bom_components if c.ref_des == ref_des]
            mpn = matches[0] if matches else None
            
            coords = _get_pdf_coords({ref_des})
            if not coords:
                return ResolvedSelection(
                    kind=ResolutionKind.ABSENT_FROM_PDF if pdf_sf else ResolutionKind.NO_PDF,
                    mpn=mpn or ref_des,  # Fallback to ref_des if no mpn found, though it's technically wrong shape? wait, if mpn is None, UNKNOWN_MPN
                    mpn_set=None,
                    ref_des_list=(ref_des,),
                    coords=()
                ) if mpn else ResolvedSelection(
                    kind=ResolutionKind.UNKNOWN_MPN,
                    mpn=ref_des, # Hack to pass non-empty string as required
                    mpn_set=None,
                    ref_des_list=(ref_des,),
                    coords=()
                )
            
            return ResolvedSelection(
                kind=ResolutionKind.SINGLE_REFDES,
                mpn=mpn or ref_des,
                mpn_set=None,
                ref_des_list=(ref_des,),
                coords=coords
            )

        elif intent.kind == SelectionKind.BOM_MPN_SET:
            mpn_set = intent.mpn_set
            # Union ref_des
            ref_des_list = sorted(set(c.ref_des for c in bom_components if c.component_mpn in mpn_set))
            coords = _get_pdf_coords(set(ref_des_list))
            
            if not coords:
                return ResolvedSelection(
                    kind=ResolutionKind.MULTI_MPN_GROUP_ABSENT,
                    mpn=None,
                    mpn_set=mpn_set,
                    ref_des_list=tuple(ref_des_list),
                    coords=()
                )
            else:
                return ResolvedSelection(
                    kind=ResolutionKind.MULTI_MPN_GROUP,
                    mpn=None,
                    mpn_set=mpn_set,
                    ref_des_list=tuple(ref_des_list),
                    coords=coords
                )

        # Original THT_MPN / MPN_CELL logic
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

        coords = _get_pdf_coords(set(ref_des_list))
        n = len(ref_des_list)
        k = len(coords)

        if intent.kind == SelectionKind.THT_MPN:
            if n == 1 and k >= 1:
                return ResolvedSelection(
                    kind=ResolutionKind.SINGLE_REFDES,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=coords
                )
            elif n == 1 and k == 0:
                return ResolvedSelection(
                    kind=ResolutionKind.ABSENT_FROM_PDF,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=()
                )
            elif n > 1:
                return ResolvedSelection(
                    kind=ResolutionKind.MULTI_REFDES,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=()
                )

        elif intent.kind == SelectionKind.MPN_CELL:
            if k == 0:
                return ResolvedSelection(
                    kind=ResolutionKind.GROUP_ABSENT,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=()
                )
            else:
                return ResolvedSelection(
                    kind=ResolutionKind.GROUP_REFDES,
                    mpn=mpn,
                    mpn_set=None,
                    ref_des_list=ref_des_list,
                    coords=coords
                )

        return ResolvedSelection(
            kind=ResolutionKind.UNKNOWN_MPN,
            mpn=mpn,
            mpn_set=None,
            ref_des_list=ref_des_list,
            coords=()
        )
