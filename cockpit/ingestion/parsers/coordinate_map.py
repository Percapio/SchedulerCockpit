"""Coordinate map loader and validator."""

import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Literal

from ..errors import CoordinateMapError


@dataclass(frozen=True)
class CoordinateAnchor:
    field_key: str
    anchor_cell: str
    anchor_text: str | list[str]
    value_offset: tuple[int, int]
    value_type: Literal["string", "integer", "date"] | None = None
    required: bool = False


@dataclass(frozen=True)
class IdentityMapping:
    part_number_field: str
    work_order_ref_field: str
    quantity_field: str


@dataclass(frozen=True)
class TravelerCoordinateMap:
    version: int
    template_revisions: list[str]
    sheet_name: str
    anchors: list[CoordinateAnchor]
    identity_mapping: IdentityMapping


def load(path: pathlib.Path | None = None) -> TravelerCoordinateMap:
    """Load and validate the JSON coordinate map."""
    source_label = path or "packaged-default"
    
    if not path:
        env_path = os.environ.get("COCKPIT_TRAVELER_MAP_PATH")
        if env_path:
            path = pathlib.Path(env_path)
            source_label = path
        else:
            from cockpit.ui.runtime import bundled_resource
            path = bundled_resource("ingestion/config/default_traveler_map.json")
            
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except Exception as e:
        raise CoordinateMapError(source_label, "JSON_LOAD_FAILED", {"error": str(e)})

    # Basic shape validation
    try:
        version = int(raw_data["version"])
        template_revisions = [str(r) for r in raw_data.get("template_revisions", [])]
        sheet_name = str(raw_data["sheet_name"])
        
        raw_identity = raw_data["identity_mapping"]
        identity_mapping = IdentityMapping(
            part_number_field=str(raw_identity["part_number_field"]),
            work_order_ref_field=str(raw_identity["work_order_ref_field"]),
            quantity_field=str(raw_identity["quantity_field"])
        )
        
        anchors = []
        for raw_anchor in raw_data["anchors"]:
            anchor_text = raw_anchor["anchor_text"]
            if not isinstance(anchor_text, (str, list)):
                raise ValueError(f"anchor_text must be str or list of str, got {type(anchor_text)}")
            if isinstance(anchor_text, list):
                anchor_text = [str(x) for x in anchor_text]
                
            offset = raw_anchor["value_offset"]
            if not isinstance(offset, list) or len(offset) != 2:
                raise ValueError("value_offset must be a 2-element list")
                
            anchors.append(CoordinateAnchor(
                field_key=str(raw_anchor["field_key"]),
                anchor_cell=str(raw_anchor["anchor_cell"]),
                anchor_text=anchor_text,
                value_offset=(int(offset[0]), int(offset[1])),
                value_type=raw_anchor.get("value_type"),
                required=bool(raw_anchor.get("required", False))
            ))
            
    except KeyError as e:
        raise CoordinateMapError(source_label, "MISSING_REQUIRED_KEY", {"key": str(e)})
    except Exception as e:
        raise CoordinateMapError(source_label, "SCHEMA_VALIDATION_FAILED", {"error": str(e)})

    # Semantic validation
    field_keys = {a.field_key for a in anchors}
    for identity_field in [identity_mapping.part_number_field, identity_mapping.work_order_ref_field, identity_mapping.quantity_field]:
        if identity_field not in field_keys:
            raise CoordinateMapError(source_label, "IDENTITY_FIELD_MISSING_FROM_ANCHORS", {"field": identity_field})
            
    for anchor in anchors:
        if anchor.field_key in [identity_mapping.part_number_field, identity_mapping.work_order_ref_field, identity_mapping.quantity_field]:
            if not anchor.required:
                raise CoordinateMapError(source_label, "IDENTITY_FIELD_NOT_REQUIRED", {"field_key": anchor.field_key})
                
    return TravelerCoordinateMap(
        version=version,
        template_revisions=template_revisions,
        sheet_name=sheet_name,
        anchors=anchors,
        identity_mapping=identity_mapping
    )
