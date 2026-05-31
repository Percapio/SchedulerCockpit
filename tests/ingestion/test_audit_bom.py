import pytest
from cockpit.ingestion.parsers.audit_bom import _split_ref_des

def test_split_ref_des_period_accepted():
    assert _split_ref_des("A1.R1, A1.R2") == ("A1.R1", "A1.R2")

def test_split_ref_des_lowercase_accepted():
    assert _split_ref_des("r1, c2") == ("r1", "c2")

def test_split_ref_des_wrappers_stripped_with_content():
    assert _split_ref_des("R1(2), R3") == ("R1", "R3")
    assert _split_ref_des("{DNP}R5") == ("R5",)
    assert _split_ref_des("[note]R7, R8") == ("R7", "R8")
    assert _split_ref_des("R1*alt*") == ("R1",)

def test_split_ref_des_commas_and_hyphens_inside_wrappers_neutralized():
    assert _split_ref_des("R1(a, b), R2") == ("R1", "R2")
    assert _split_ref_des("R1[sub-part], R2") == ("R1", "R2")

def test_split_ref_des_unbalanced_wrapper_fails():
    with pytest.raises(ValueError, match="INVALID_REFDES_TOKEN"):
        _split_ref_des("R1(no-close")

def test_split_ref_des_nested_wrapper_deterministically_fails():
    with pytest.raises(ValueError, match="INVALID_REFDES_TOKEN"):
        _split_ref_des("R1(a(b))")

def test_split_ref_des_symbol_only_token_passes():
    assert _split_ref_des(".") == (".",)
    assert _split_ref_des("-") == ("-",)

def test_split_ref_des_hyphen_unconstrained():
    assert _split_ref_des("R1-R5") == ("R1-R5",)
    assert _split_ref_des("-R1, R2-") == ("-R1", "R2-")
