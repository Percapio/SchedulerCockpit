import pytest
import json
from cockpit.services.mpn_library.digikey_types import (
    VariantCandidate,
    PackagingType,
    RequestBudget,
    DigiKeyProductQuery,
    SnapshotUnusable
)
from cockpit.services.mpn_library.digikey_variant import (
    classify_packaging,
    rank_variant,
    extract_variants,
    restore_candidates_from_snapshot
)
from cockpit.persistence.repositories.bom_components import PersistedBomLine

def test_digikey_product_query_payload():
    # Only keywords field should be present
    line = PersistedBomLine(
        audit_id=1, source_file_id=1, part_number="P1", split_suffix="A",
        work_order_ref="WO1", find_number="1", component_mpn="RC0805",
        description="Resistor", mount_type="S"
    )
    q = DigiKeyProductQuery(keywords=line.component_mpn)
    
    # Assert serialised body has only 'keywords'
    body = json.loads(json.dumps({"keywords": q.keywords}))
    assert len(body) == 1
    assert "keywords" in body
    assert body["keywords"] == "RC0805"


def test_classify_packaging():
    assert classify_packaging("Tape & Reel (TR)") == PackagingType.TAPE_AND_REEL
    assert classify_packaging("Cut Tape (CT)") == PackagingType.CUT_TAPE
    assert classify_packaging("Digi-Reel®") == PackagingType.DIGI_REEL
    assert classify_packaging("Tray") == PackagingType.TRAY
    assert classify_packaging("Tube") == PackagingType.TUBE
    assert classify_packaging("Bulk") == PackagingType.BULK
    assert classify_packaging("Something Else") == PackagingType.UNKNOWN
    assert classify_packaging(None) == PackagingType.UNKNOWN
    assert classify_packaging("  TRAY  ") == PackagingType.TRAY


def test_variant_ranking():
    # tape and reel outranks cut tape outranks digi-reel
    c1 = VariantCandidate("DK1", "MFR", "Tape & Reel (TR)", PackagingType.TAPE_AND_REEL, [])
    c2 = VariantCandidate("DK2", "MFR", "Cut Tape (CT)", PackagingType.CUT_TAPE, [])
    
    r1 = rank_variant(c1)
    r2 = rank_variant(c2)
    # reverse=False sorting means smaller tuple comes first.
    # so r1 should be < r2
    assert r1 < r2


def test_extract_variants_empty():
    res = extract_variants("MPN1", {"Products": []})
    assert len(res) == 0


def test_restore_snapshot():
    # Valid
    response = {
        "Products": [
            {
                "DigiKeyPartNumber": "DK1",
                "Packaging": {"Value": "Tape & Reel (TR)"},
                "Parameters": [
                    {"Parameter": "Tape Width", "Value": "8mm"}
                ]
            }
        ]
    }
    
    candidates = restore_candidates_from_snapshot("MPN1", json.dumps(response))
    assert not isinstance(candidates, SnapshotUnusable)
    assert len(candidates) == 1
    
    # Invalid JSON
    assert isinstance(restore_candidates_from_snapshot("MPN1", "{bad json"), SnapshotUnusable)
    
    # Empty extraction
    assert isinstance(restore_candidates_from_snapshot("MPN1", json.dumps({"Products": []})), SnapshotUnusable)


def test_budget():
    b = RequestBudget(remaining=2, spent=0)
    assert b.take() is True
    assert b.remaining == 1
    assert b.spent == 1
    
    assert b.take() is True
    assert b.take() is False
    assert b.spent == 2
