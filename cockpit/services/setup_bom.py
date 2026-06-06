from dataclasses import dataclass
from typing import Tuple, List
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from PyQt6.QtPrintSupport import QPrinter

from cockpit.layout.constants import PAGE_SIDE_LABELS

@dataclass(frozen=True)
class SetupBomRow:
    item_number: int
    part_number: str
    description: str | None
    reference_designators: Tuple[str, ...]

class SideFilter:
    TOP = "Top"
    BOTTOM = "Bottom"
    BOTH = "Both"

class ProcessFilter:
    SMT = "SMT"
    THT = "THT"
    BOTH = "Both"

class SetupBomService:
    def __init__(self, audit_repo: AuditRepository, bom_repo: AuditBomComponentRepository, pdf_repo: PdfComponentCoordRepository, source_file_repo):
        self._audit_repo = audit_repo
        self._bom_repo = bom_repo
        self._pdf_repo = pdf_repo
        self._source_file_repo = source_file_repo

    def build(self, audit_id: int, side: str, process: str) -> List[SetupBomRow]:
        from cockpit.persistence.types import SourceFileCategory
        
        bom_sf = self._source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return []
            
        components = self._bom_repo.list_for_source_file(bom_sf.id)
        
        pdf_sf = self._source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
        coords = self._pdf_repo.list_for_source_file(pdf_sf.id) if pdf_sf is not None else []
        
        # coord mapping: ref_des -> page_index
        ref_des_to_page = {c.ref_des: c.page_index for c in coords}
        
        # group components by (find_number, component_mpn, description)
        grouped = {}
        for comp in components:
            if process != ProcessFilter.BOTH:
                expected_mount_type = 'S' if process == ProcessFilter.SMT else 'T'
                if comp.mount_type != expected_mount_type:
                    continue
            
            key = (comp.find_number, comp.component_mpn, comp.description)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(comp.ref_des)
            
        rows = []
        for key, ref_des_list in grouped.items():
            find_number, mpn, desc = key
            matching_refs = []
            
            if side == SideFilter.BOTH:
                matching_refs = ref_des_list
            else:
                target_page = 0 if side == SideFilter.TOP else 1
                for ref in ref_des_list:
                    page = ref_des_to_page.get(ref)
                    if page == target_page:
                        matching_refs.append(ref)
                
            if matching_refs:
                rows.append(SetupBomRow(
                    item_number=find_number,
                    part_number=mpn,
                    description=desc,
                    reference_designators=tuple(sorted(matching_refs))
                ))
                
        # sort by find_number
        rows.sort(key=lambda r: r.item_number)
        return rows

    def print_bom(self, rows: List[SetupBomRow], printer: QPrinter) -> None:
        from PyQt6.QtGui import QTextDocument, QFont
        import html
        
        doc = QTextDocument()
        doc.setDefaultFont(QFont("Courier", 10))
        
        html_content = ["<h1>Setup BOM</h1><table border='1' cellspacing='0' cellpadding='4'>"]
        html_content.append("<tr><th>Item</th><th>Part Number</th><th>Description</th><th>Ref Des</th></tr>")
        
        for row in rows:
            desc = html.escape(row.description or "")
            refs = html.escape(" ".join(row.reference_designators))
            html_content.append(f"<tr><td>{row.item_number}</td><td>{html.escape(row.part_number)}</td><td>{desc}</td><td>{refs}</td></tr>")
            
        html_content.append("</table>")
        doc.setHtml("".join(html_content))
        
        try:
            doc.print(printer)
        except Exception as e:
            from cockpit.services.errors import PrintError
            raise PrintError(f"Failed to print Setup BOM: {str(e)}", {"error": str(e)})
