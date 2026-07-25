import math
from typing import Tuple, Optional, Callable
import logging
from dataclasses import dataclass

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.types import SourceFileCategory
from cockpit.services.runtime_constants import RuntimeConstants

logger = logging.getLogger(__name__)

@dataclass
class RuntimeInputs:
    smt_placements: int
    smt_unique_mpns: int
    tht_placements: int
    quantity: int
    sides: int
    is_class_3: bool = False
    is_clean_process: bool = False
    ops_per_board_min: Optional[float] = None

@dataclass(frozen=True)
class RecomputeSummary:
    attempted: int
    failed: int

@dataclass(frozen=True)
class RuntimeResults:
    feeder: float
    smt: float
    tht: float
    aoi: float
    shipping: float
    ops: Optional[float] = None

def compute_tht(inputs: RuntimeInputs, constants: RuntimeConstants) -> float:
    tht_volume_hours = inputs.tht_placements * inputs.quantity * constants.tht_placement_time_min / 60
    class_3_factor = constants.class_3_multiplier_tht if inputs.is_class_3 else 1.0
    clean_factor   = constants.clean_process_multiplier_tht if inputs.is_clean_process else 1.0
    return float(tht_volume_hours * class_3_factor * clean_factor)

def compute_aoi(inputs: RuntimeInputs, constants: RuntimeConstants) -> float:
    clamped_sides = max(1, min(2, inputs.sides))
    inspection_minutes = (inputs.smt_placements * clamped_sides * inputs.quantity
                          * constants.aoi_inspection_time_min)
    inspection_hours = inspection_minutes / 60.0
    class_3_factor = constants.class_3_multiplier_aoi if inputs.is_class_3 else 1.0
    return float(math.ceil(inspection_hours * class_3_factor))

def compute_ops(inputs: RuntimeInputs, constants: RuntimeConstants) -> Optional[float]:
    if inputs.ops_per_board_min is None:
        return None
    clean_factor = constants.clean_process_multiplier_ops if inputs.is_clean_process else 1.0
    ops_minutes = inputs.ops_per_board_min * inputs.quantity
    ops_hours = ops_minutes / 60.0
    return float(math.ceil(ops_hours * clean_factor))

def compute_shipping(inputs: RuntimeInputs, constants: RuntimeConstants) -> float:
    if constants.shipping_boards_per_hour > 0:
        per_unit_hours = math.ceil(inputs.quantity / constants.shipping_boards_per_hour)
    else:
        per_unit_hours = 0
    return float(constants.shipping_flat_hours + per_unit_hours)

def compute(inputs: RuntimeInputs, constants: RuntimeConstants) -> RuntimeResults:
    feeder = float(inputs.smt_unique_mpns / 30)

    clamped_sides = max(1, min(2, inputs.sides))
    base = (2 if inputs.smt_placements > 1000 else 0) + (2 if inputs.smt_unique_mpns > 115 else 0)
    smt_volume = (inputs.quantity * inputs.smt_placements * constants.smt_placement_time_min / 60)
    smt = float(smt_volume + clamped_sides + base)

    tht = compute_tht(inputs, constants)
    aoi = compute_aoi(inputs, constants)
    ops = compute_ops(inputs, constants)
    shipping = compute_shipping(inputs, constants)

    return RuntimeResults(feeder=feeder, smt=smt, tht=tht, aoi=aoi, shipping=shipping, ops=ops)

class RuntimeCalcService:
    def __init__(
        self,
        audit_repo: AuditRepository,
        source_file_repo: SourceFileRepository,
        bom_repo: AuditBomComponentRepository,
        pdf_repo: PdfComponentCoordRepository,
        constants_provider: Callable[[], RuntimeConstants] = RuntimeConstants.defaults
    ):
        self._audit_repo = audit_repo
        self._source_file_repo = source_file_repo
        self._bom_repo = bom_repo
        self._pdf_repo = pdf_repo
        self._constants_provider = constants_provider

    def _pdf_page_count(self, audit_id: int) -> int:
        pdf_sf = self._source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
        if pdf_sf is None:
            return 1
            
        try:
            coords = self._pdf_repo.list_for_source_file(pdf_sf.id)
            if not coords:
                return 1
            max_page = max(c.page_index for c in coords)
            return max_page + 1
        except Exception as e:
            logger.warning(f"Failed to determine PDF page count for audit {audit_id}: {e}")
            return 1

    def gather_inputs(self, audit_id: int, known_pdf_page_count: Optional[int] = None) -> Optional[RuntimeInputs]:
        audit = self._audit_repo.find_by_id(audit_id)
        if not audit:
            return None

        bom_sf = self._source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return None

        components = self._bom_repo.list_for_source_file(bom_sf.id)
        
        smt_comps = [c for c in components if c.mount_type == 'S']
        tht_comps = [c for c in components if c.mount_type == 'T']
        
        sides = known_pdf_page_count if known_pdf_page_count is not None else self._pdf_page_count(audit_id)
        
        return RuntimeInputs(
            smt_placements=len(smt_comps),
            smt_unique_mpns=len({c.component_mpn for c in smt_comps}),
            tht_placements=len(tht_comps),
            quantity=audit.quantity,
            sides=sides,
            is_class_3=audit.is_class_3,
            is_clean_process=audit.is_clean_process,
            ops_per_board_min=audit.ops_per_board_min,
        )

    def recompute_all(self) -> RecomputeSummary:
        attempted = 0
        failed = 0
        try:
            audit_ids = self._audit_repo.all_active_ids()
        except Exception as enumerate_error:
            logger.error("recompute_all: could not enumerate audits: %s", enumerate_error)
            return RecomputeSummary(attempted=0, failed=0)
            
        for audit_id in audit_ids:
            attempted += 1
            try:
                self.persist(audit_id)
            except Exception as persist_error:
                failed += 1
                logger.error("recompute_all: audit %s failed: %s", audit_id, persist_error)
        return RecomputeSummary(attempted=attempted, failed=failed)

    def persist(self, audit_id: int, known_pdf_page_count: Optional[int] = None) -> None:
        inputs = self.gather_inputs(audit_id, known_pdf_page_count)
        if inputs is None:
            return
            
        constants = self._constants_provider()
        results = compute(inputs, constants)
        
        cur = self._audit_repo.conn.cursor()
        cur.execute(
            """
            UPDATE active_audits 
            SET feeder_setuptime = ?, smt_runtime = ?, tht_runtime = ?, 
                aoi_runtime = ?, ops_runtime = ?, shipping_runtime = ?
            WHERE id = ?
            """,
            (results.feeder, results.smt, results.tht, 
             results.aoi, results.ops, results.shipping, audit_id)
        )
