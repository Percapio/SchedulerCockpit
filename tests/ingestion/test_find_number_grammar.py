import pytest
import datetime
from pathlib import Path
from cockpit.ingestion.parsers.audit_bom import coerce_find_number
from cockpit.ingestion.errors import MalformedBomError

def test_find_number_grammar_valid_numbers():
    path = Path("fake.xlsx")
    assert coerce_find_number(37, path, "MPN") == "37"
    assert coerce_find_number(37.0, path, "MPN") == "37"

def test_find_number_grammar_valid_alphanumeric():
    path = Path("fake.xlsx")
    assert coerce_find_number("37a", path, "MPN") == "37A"
    assert coerce_find_number("37A", path, "MPN") == "37A"

def test_find_number_grammar_invalid_values():
    path = Path("fake.xlsx")
    for val in ["37AB", "A37", "37.5", "see note", datetime.datetime.now()]:
        with pytest.raises(MalformedBomError) as excinfo:
            coerce_find_number(val, path, "MPN")
        assert excinfo.value.reason == "INVALID_FIND_NUMBER"

def test_find_number_grammar_zeroes():
    path = Path("fake.xlsx")
    for val in ["0", "00", "0A", 0]:
        with pytest.raises(MalformedBomError) as excinfo:
            coerce_find_number(val, path, "MPN")
        assert excinfo.value.reason == "INVALID_FIND_NUMBER"

def test_find_number_grammar_boolean():
    path = Path("fake.xlsx")
    for val in [True, False]:
        with pytest.raises(MalformedBomError) as excinfo:
            coerce_find_number(val, path, "MPN")
        assert excinfo.value.reason == "INVALID_FIND_NUMBER"

def test_find_number_grammar_missing():
    path = Path("fake.xlsx")
    for val in [None, "   "]:
        with pytest.raises(MalformedBomError) as excinfo:
            coerce_find_number(val, path, "MPN")
        assert excinfo.value.reason == "MISSING_FIND_NUMBER"
