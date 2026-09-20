import json
import logging
import re
from typing import List, Tuple, Union, Optional
from .types import MpnKey, PartAttributeDraft, Provenance
from .digikey_types import (
    PackagingType,
    VariantCandidate,
    SnapshotUnusable
)
from .registry import ATTRIBUTE_REGISTRY

logger = logging.getLogger(__name__)

# Map DigiKey parameter names to our registry names.
#
# One registry name is supplied by at most one label per product record. A
# record carrying two labels for one name is a defect in this table, not a
# runtime condition, and fails the projection test rather than resolving by
# iteration order.
#
# "Standard Package" is deliberately absent: it is DigiKey's order multiple,
# not the quantity on a reel, and for a cut-tape variant the two differ by
# orders of magnitude. No registry name holds an order multiple.
#
# pin_count, body_length_mm and body_width_mm are deliberately absent: the
# Phase 48 snapshot survey found no retained response to confirm a label
# against. See scripts/survey_parameter_labels.py.
PARAM_MAPPING = {
    "Tape Width": "carrier_width_mm",
    "Carrier Width": "carrier_width_mm",
    "Tape Pitch": "carrier_pitch_mm",
    "Carrier Pitch": "carrier_pitch_mm",
    "Reel Diameter": "reel_diameter_mm",
    "Quantity per Reel": "quantity_per_reel",
    "Package / Case": "package_case",
    "Supplier Device Package": "supplier_device_package",
    "Mounting Type": "mounting_type",
    "Moisture Sensitivity Level (MSL)": "moisture_sensitivity_level",
}

def _parse_numeric(val: str) -> Optional[float]:
    # Extract first float/int from string
    match = re.search(r"[-+]?\d*\.\d+|\d+", val)
    if match:
        return float(match.group())
    return None

def classify_packaging(packaging_label: Optional[str]) -> PackagingType:
    if packaging_label is None:
        return PackagingType.UNKNOWN

    label_upper = packaging_label.strip().upper()

    if label_upper == "TAPE & REEL (TR)":
        return PackagingType.TAPE_AND_REEL
    elif label_upper == "CUT TAPE (CT)":
        return PackagingType.CUT_TAPE
    elif label_upper == "DIGI-REEL®" or label_upper == "DIGI-REEL\u00ae":
        return PackagingType.DIGI_REEL
    elif label_upper == "TRAY":
        return PackagingType.TRAY
    elif label_upper == "TUBE":
        return PackagingType.TUBE
    elif label_upper == "BULK":
        return PackagingType.BULK

    return PackagingType.UNKNOWN


def extract_variants(mpn_key: MpnKey, response_json: dict) -> List[VariantCandidate]:
    """
    Reduces a product search response to the variants worth offering.
    Only returns candidates supplying at least one CARRIER-group attribute.
    Ordered by rank_variant descending.
    """
    candidates = []
    
    products = response_json.get("Products", [])
    if not isinstance(products, list):
        return []

    for product in products:
        dk_pn = product.get("DigiKeyPartNumber")
        if not dk_pn:
            continue
            
        mfr = product.get("Manufacturer", {}).get("Value")
        pkg_label = product.get("Packaging", {}).get("Value")
        
        pkg_type = classify_packaging(pkg_label)
        
        attributes = []
        parameters = product.get("Parameters", [])
        if isinstance(parameters, list):
            for param in parameters:
                param_name = param.get("Parameter")
                param_val = param.get("Value")
                if not param_name or not param_val or param_val == "-":
                    continue
                    
                reg_name = PARAM_MAPPING.get(param_name)
                if not reg_name or reg_name not in ATTRIBUTE_REGISTRY:
                    continue
                    
                spec = ATTRIBUTE_REGISTRY[reg_name]
                numeric_val = _parse_numeric(param_val) if spec.value_kind.value == "NUMERIC" else None
                
                attributes.append(PartAttributeDraft(
                    mpn_key=mpn_key,
                    attribute_name=reg_name,
                    provenance=Provenance.DIGIKEY,
                    value_text=param_val,
                    value_numeric=numeric_val,
                    source_label=param_name
                ))
        
        has_carrier = any(
            attr.attribute_name in ATTRIBUTE_REGISTRY and 
            ATTRIBUTE_REGISTRY[attr.attribute_name].group.value == "CARRIER"
            for attr in attributes
        )
        
        if has_carrier:
            candidates.append(VariantCandidate(
                digikey_part_number=dk_pn,
                manufacturer=mfr,
                packaging_label=pkg_label,
                packaging_type=pkg_type,
                attributes=attributes
            ))

    # sort uses rank_variant which returns a tuple, python sorts lexicographically
    # we want descending rank, so reverse=False since rank_variant negates the scores
    return sorted(candidates, key=rank_variant, reverse=False)


def rank_variant(candidate: VariantCandidate) -> Tuple[int, int, str]:
    """
    Sort key placing the variant an SMT programmer most likely wants highest.
    (packaging priority, populated CARRIER attributes descending, part number ascending)
    Returns a tuple to be used with reverse=False.
    """
    priority = {
        PackagingType.TAPE_AND_REEL: 6,
        PackagingType.CUT_TAPE: 5,
        PackagingType.DIGI_REEL: 4,
        PackagingType.TRAY: 3,
        PackagingType.TUBE: 2,
        PackagingType.BULK: 1,
        PackagingType.UNKNOWN: 0
    }
    pkg_score = priority.get(candidate.packaging_type, 0)
    
    carrier_count = sum(
        1 for attr in candidate.attributes 
        if attr.attribute_name in ATTRIBUTE_REGISTRY and ATTRIBUTE_REGISTRY[attr.attribute_name].group.value == "CARRIER"
    )
    
    # We want max pkg_score, max carrier_count, min dk_pn.
    return (-pkg_score, -carrier_count, candidate.digikey_part_number)


def restore_candidates_from_snapshot(mpn_key: MpnKey, snapshot_json: str) -> Union[List[VariantCandidate], SnapshotUnusable]:
    try:
        data = json.loads(snapshot_json)
        candidates = extract_variants(mpn_key, data)
        # extract_variants returns sorted candidates.
        # Wait, the spec says "returns SnapshotUnusable when... extraction yields an empty list"
        if not candidates:
            return SnapshotUnusable()
        return candidates
    except (json.JSONDecodeError, TypeError):
        return SnapshotUnusable()
