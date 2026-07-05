import pytest
from cockpit.services.itar import is_itar, classification_display

def test_is_itar_true():
    assert is_itar({"itar_classification": "YES"}) is True
    assert is_itar({"itar_classification": "Y"}) is True
    assert is_itar({"itar_classification": "ITAR"}) is True

def test_is_itar_false():
    assert is_itar({"itar_classification": "NO"}) is False
    assert is_itar({"itar_classification": None}) is False
    assert is_itar({}) is False
    assert is_itar(None) is False

def test_classification_display():
    assert classification_display({"itar_classification": "YES"}) == "ITAR"
    assert classification_display({"itar_classification": "NO"}) == "Non-ITAR"

def test_parser_anchor_matches_itar():
    import json
    import pathlib
    config_path = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ingestion" / "config" / "default_traveler_map.json"
    data = json.loads(config_path.read_text())
    
    # Ensure itar_classification anchor has both texts
    anchor = next((a for a in data["anchors"] if a["field_key"] == "itar_classification"), None)
    assert anchor is not None
    assert "ITAR:" in anchor["anchor_text"]
    assert "CLASSIFICATION:" in anchor["anchor_text"]
