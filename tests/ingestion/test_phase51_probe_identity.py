"""Phase 51 section 3.2 — the pre-flight identity probe."""

import pathlib
from types import SimpleNamespace

import pytest

from cockpit.ingestion import hashing
from cockpit.ingestion.errors import CrossValidationError
from cockpit.ingestion.service import probe_identity


class _Mapping:
    part_number_field = "assembly_number"
    work_order_ref_field = "sales_order_number"
    quantity_field = "quantity"


class _Map:
    identity_mapping = _Mapping()


@pytest.fixture
def traveler_file(tmp_path):
    path = tmp_path / "B123456 MFG Traveler.xlsx"
    path.write_bytes(b"traveler bytes")
    return path


def stub_traveler(monkeypatch, fields, seen=None):
    def parse(path, coord_map):
        if seen is not None:
            seen.append(("traveler", path))
        return SimpleNamespace(extracted_fields=fields)

    monkeypatch.setattr("cockpit.ingestion.parsers.traveler.parse", parse)


def test_returns_the_identity_and_pins_the_bytes(monkeypatch, traveler_file):
    stub_traveler(monkeypatch, {
        "assembly_number": "B139092", "sales_order_number": "16026", "quantity": 100,
    })

    probed = probe_identity(traveler_file, _Map())

    assert probed.part_number == "B139092"
    assert probed.work_order_ref == "16026"
    assert probed.quantity == 100
    assert probed.traveler_hash == hashing.sha256_hex(traveler_file)


def test_only_the_traveler_is_parsed(monkeypatch, traveler_file):
    """The BOM and ECO contribute nothing to identity."""
    seen = []
    stub_traveler(monkeypatch, {
        "assembly_number": "B139092", "sales_order_number": "16026", "quantity": 1,
    }, seen)

    def boom(*args, **kwargs):
        raise AssertionError("the pre-flight must not parse this")

    monkeypatch.setattr("cockpit.ingestion.parsers.audit_bom.parse", boom)
    monkeypatch.setattr("cockpit.ingestion.parsers.eco_build_notes.parse", boom)

    probe_identity(traveler_file, _Map())

    assert [kind for kind, _ in seen] == ["traveler"]


def test_nothing_is_written(monkeypatch, tmp_path, traveler_file):
    stub_traveler(monkeypatch, {
        "assembly_number": "B139092", "sales_order_number": "16026", "quantity": 1,
    })
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}

    probe_identity(traveler_file, _Map())

    after = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    assert before == after


def test_values_are_trimmed(monkeypatch, traveler_file):
    stub_traveler(monkeypatch, {
        "assembly_number": "  B139092 ", "sales_order_number": " 16026  ", "quantity": 5,
    })

    probed = probe_identity(traveler_file, _Map())

    assert probed.part_number == "B139092"
    assert probed.work_order_ref == "16026"


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"assembly_number": "", "sales_order_number": "16026", "quantity": 1},
         "PART_NUMBER_MISSING"),
        ({"assembly_number": "B1", "sales_order_number": "", "quantity": 1},
         "WORK_ORDER_MISSING"),
        ({"assembly_number": "B1", "sales_order_number": "16026", "quantity": 0},
         "QUANTITY_INVALID"),
        ({"assembly_number": "B1", "sales_order_number": "16026", "quantity": "many"},
         "QUANTITY_INVALID"),
        ({}, "PART_NUMBER_MISSING"),
    ],
)
def test_an_unusable_traveler_is_refused(monkeypatch, traveler_file, fields, reason):
    stub_traveler(monkeypatch, fields)

    with pytest.raises(CrossValidationError) as excinfo:
        probe_identity(traveler_file, _Map())

    assert excinfo.value.reason == reason
