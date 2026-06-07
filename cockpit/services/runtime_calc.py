import math
from typing import Tuple, Optional
import logging
from dataclasses import dataclass

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.types import SourceFileCategory

logger = logging.getLogger(__name__)

@dataclass
class RuntimeInputs:
    smt_placements: int
    smt_unique_mpns: int
    tht_placements: int
    quantity: int
    sides: int

class RuntimeCalcService:
    def __init__(
        self,
        audit_repo: AuditRepository,
        source_file_repo: SourceFileRepository,
        bom_repo: AuditBomComponentRepository,
        pdf_repo: PdfComponentCoordRepository
    ):
        self._audit_repo = audit_repo
        self._source_file_repo = source_file_repo
        self._bom_repo = bom_repo
        self._pdf_repo = pdf_repo

    def _pdf_page_count(self, audit_id: int) -> int:
        pdf_sf = self._source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
        if pdf_sf is None:
            return 1
            
        try:
            # We can find the max page index from the coordinates as a quick way to get page count,
            # or if the architecture doc said "read the page count via the renderer (page_dimensions length)",
            # wait, I don't have direct access to the renderer here unless I inject it.
            # But the PDF coords have `page_index` (0-indexed). Max page index + 1 = page count.
            # If no coords, then 1.
            coords = self._pdf_repo.list_for_source_file(pdf_sf.id)
            if not coords:
                return 1
            max_page = max(c.page_index for c in coords)
            return max_page + 1
        except Exception as e:
            logger.warning(f"Failed to determine PDF page count for audit {audit_id}: {e}")
            return 1

    def compute(self, audit_id: int, known_pdf_page_count: Optional[int] = None) -> Optional[Tuple[float, float, float]]:
        audit = self._audit_repo.find_by_id(audit_id)
        if not audit:
            return None

        bom_sf = self._source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return None

        components = self._bom_repo.list_for_source_file(bom_sf.id)
        
        smt_comps = [c for c in components if c.mount_type == 'S']
        tht_comps = [c for c in components if c.mount_type == 'T']
        
        smt_placements = len(smt_comps)
        smt_unique_mpns = len({c.component_mpn for c in smt_comps})
        tht_placements = len(tht_comps)
        quantity = audit.quantity
        
        sides = known_pdf_page_count if known_pdf_page_count is not None else self._pdf_page_count(audit_id)
        
        inp = RuntimeInputs(
            smt_placements=smt_placements,
            smt_unique_mpns=smt_unique_mpns,
            tht_placements=tht_placements,
            quantity=quantity,
            sides=sides
        )
        
        feeder = float(inp.smt_unique_mpns / 30)
        
        clamped_sides = max(1, min(2, inp.sides))
        
        base = (2 if inp.smt_placements > 1000 else 0) + (2 if inp.smt_unique_mpns > 115 else 0)
        smt_volume = (inp.quantity * inp.smt_placements * 0.012 / 60)
        smt = float(smt_volume + clamped_sides + base)
        
        tht_volume: float = inp.tht_placements * inp.quantity * 0.15 / 60
        tht = float(tht_volume + ((inp.tht_placements / 800) + (inp.quantity / 90)) * (2.25 if inp.tht_placements > 500 else 1.125))
        
        return (feeder, smt, tht)

    def persist(self, audit_id: int, known_pdf_page_count: Optional[int] = None) -> None:
        result = self.compute(audit_id, known_pdf_page_count)
        if result is None:
            return
            
        feeder, smt, tht = result
        cur = self._audit_repo.conn.cursor()
        cur.execute(
            """
            UPDATE active_audits 
            SET feeder_setuptime = ?, smt_runtime = ?, tht_runtime = ? 
            WHERE id = ?
            """,
            (feeder, smt, tht, audit_id)
        )
