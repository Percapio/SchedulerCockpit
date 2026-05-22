"""Cross-validation module."""

import datetime
from typing import Any

from cockpit.persistence.types import ActiveAuditDraft
from .errors import CrossValidationError
from .parsers.coordinate_map import TravelerCoordinateMap
from .parsers.results import BomResult, EcoResult, IngestionIntent, TravelerResult


def _sanitize_for_json(data: dict[str, Any]) -> dict[str, Any]:
    """Sanitize extracted fields for JSON serialization."""
    sanitized = {}
    for k, v in data.items():
        if isinstance(v, (datetime.datetime, datetime.date)):
            sanitized[k] = v.isoformat()
        else:
            sanitized[k] = v
    return sanitized


def reconcile(
    bom: BomResult,
    eco: EcoResult,
    traveler: TravelerResult,
    coord_map: TravelerCoordinateMap
) -> IngestionIntent:
    """Reconcile parser outputs and enforce cross-file consistency."""
    
    mapping = coord_map.identity_mapping
    
    trav_part_number = traveler.extracted_fields.get(mapping.part_number_field)
    if trav_part_number is None:
        trav_part_number = ""
    trav_part_number = str(trav_part_number).strip()

    bom_pn = bom.declared_part_number.strip()
    eco_pn = eco.declared_part_number.strip()

    if not (bom_pn == eco_pn == trav_part_number):
        raise CrossValidationError("PART_NUMBER_MISMATCH", {
            "bom": bom_pn,
            "eco": eco_pn,
            "traveler": trav_part_number
        })

    work_order = traveler.extracted_fields.get(mapping.work_order_ref_field)
    if not work_order or not str(work_order).strip():
        raise CrossValidationError("WORK_ORDER_MISSING", {
            "field": mapping.work_order_ref_field
        })
    work_order_str = str(work_order).strip()

    quantity = traveler.extracted_fields.get(mapping.quantity_field)
    if not isinstance(quantity, int) or quantity < 1:
        raise CrossValidationError("QUANTITY_INVALID", {
            "value": quantity,
            "field": mapping.quantity_field
        })

    audit_draft = ActiveAuditDraft(
        part_number=trav_part_number,
        work_order_ref=work_order_str,
        quantity=quantity,
        split_suffix="",
        schedule_job_id=None,
        traveler_metadata=_sanitize_for_json(traveler.extracted_fields)
    )

    return IngestionIntent(
        audit_draft=audit_draft,
        bom_items=bom.items,
        eco_items=eco.items
    )
