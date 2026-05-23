"""JSON contract for layout parser results."""

from typing import Any

from .results import PdfLayoutResult


def to_json_contract(result: PdfLayoutResult) -> dict[str, Any]:
    """Convert PdfLayoutResult to a JSON-serializable contract."""
    return {
        "page_count": result.page_count,
        "found_ref_des": sorted(list(result.found_ref_des)),
        "coordinates": [
            {
                "ref_des": c.ref_des,
                "page_index": c.page_index,
                "x1": c.x1,
                "y1": c.y1,
                "x2": c.x2,
                "y2": c.y2
            }
            for c in result.coordinates
        ]
    }
