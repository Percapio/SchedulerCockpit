"""Phase 49 section 8.2 — the Wash column's source of truth.

The Audit List reads the traveler's own process_clean wording through
wash_state, not the is_clean_process boolean. The boolean asks only whether the
cell is non-empty, so it cannot tell "CLEAN" from "NO CLEAN"; it remains the
input to runtime_calc's clean factor and is characterised here so the two are
not confused for each other.
"""

import pytest

from cockpit.persistence.traveler_flags import WashState, is_clean_process, wash_state
from tests.services.test_ingestion_service import ingestion_service  # noqa: F401


def _ingest_with_metadata(service, tmp_path, monkeypatch, metadata):
    from cockpit.ingestion.parsers.results import BomItem, IngestionIntent
    from cockpit.persistence.types import ActiveAuditDraft

    for name in ("B123456 AUDIT BOM.xlsx", "B123456 Traveler.xlsx", "B123456 ECO.docx"):
        (tmp_path / name).write_text("a")
    paths = [
        tmp_path / "B123456 AUDIT BOM.xlsx",
        tmp_path / "B123456 Traveler.xlsx",
        tmp_path / "B123456 ECO.docx",
    ]

    monkeypatch.setattr("cockpit.ingestion.parsers.audit_bom.parse", lambda p: None)
    monkeypatch.setattr(
        "cockpit.ingestion.parsers.eco_build_notes.parse",
        lambda p: type("EcoResult", (), {"declared_part_number": "", "row_count": 1, "raw_table_count": 1})(),
    )
    monkeypatch.setattr("cockpit.ingestion.parsers.traveler.parse", lambda p, cm: None)

    intent = IngestionIntent(
        audit_draft=ActiveAuditDraft(
            part_number="B123456",
            work_order_ref="WO",
            quantity=1,
            traveler_metadata=metadata,
        ),
        bom_items=[
            BomItem(
                component_mpn="SMT-1",
                description="SMT Component",
                mount_type="S",
                ref_des_raw="C1",
                ref_des_list=("C1",),
                find_number="1",
            )
        ],
    )
    monkeypatch.setattr(
        "cockpit.ingestion.cross_validation.reconcile", lambda b, e, t, cm: intent
    )
    return service.ingest(paths)


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"process_clean": "CLEAN"}, True),
        ({"process_clean": ""}, False),
        ({}, False),
        # Whitespace-only is stripped before the emptiness test, so it reads as
        # no-clean rather than as a declared wash process.
        ({"process_clean": "   "}, False),
        # Characterisation, not endorsement: the predicate tests only that the
        # field is non-empty, so a traveler that spells out "NO CLEAN" is read
        # as a wash process. The Wash column no longer depends on this.
        ({"process_clean": "NO CLEAN"}, True),
    ],
)
def test_column_agrees_with_the_predicate_after_a_real_ingest(
    ingestion_service, monkeypatch, metadata, expected  # noqa: F811
):
    service, _, tmp_path = ingestion_service
    audit = _ingest_with_metadata(service, tmp_path, monkeypatch, metadata)

    stored = service.audit_repo.find_by_id(audit.id)

    assert stored.is_clean_process is expected
    assert stored.is_clean_process == is_clean_process(metadata)


def test_unparsed_traveler_is_indistinguishable_from_no_clean():
    """Section 3.1: NC asserts slightly more than the data supports.

    Recorded as a test so the limitation is visible rather than discovered.
    """
    assert is_clean_process({}) is False
    assert is_clean_process({"process_clean": ""}) is False


def test_no_clean_text_is_read_as_a_wash_process_by_the_boolean():
    """Characterises is_clean_process, which the Wash column no longer uses.

    The predicate asks only whether process_clean is non-empty, so a traveler
    spelling the no-clean case out reads as a wash process. That flag still
    drives runtime_calc's clean_process_multiplier_tht, so the consequence is
    a wrong THT runtime, not a wrong column. Locked down so a later fix has a
    failing test to flip.
    """
    assert is_clean_process({"process_clean": "NO CLEAN"}) is True
    assert is_clean_process({"process_clean": "CLEAN"}) is True


# --- wash_state, which the Audit List does use ------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("CLEAN", WashState.CLEAN),
        ("clean", WashState.CLEAN),
        ("WASH", WashState.CLEAN),
        ("AQUEOUS WASH", WashState.CLEAN),
        ("NO CLEAN", WashState.NO_CLEAN),
        ("NO-CLEAN", WashState.NO_CLEAN),
        ("NOCLEAN", WashState.NO_CLEAN),
        ("Non-Clean", WashState.NO_CLEAN),
        ("", WashState.UNKNOWN),
        ("   ", WashState.UNKNOWN),
        (None, WashState.UNKNOWN),
        (3, WashState.UNKNOWN),
    ],
)
def test_wash_state_reads_the_travelers_wording(text, expected):
    assert wash_state({"process_clean": text}) is expected


def test_wash_state_of_an_unmapped_traveler_is_unknown():
    """The state every audit ingested before 2026-06-06 is actually in."""
    assert wash_state({}) is WashState.UNKNOWN
    assert wash_state({"process": "LEAD FREE"}) is WashState.UNKNOWN
    assert wash_state(None) is WashState.UNKNOWN


def test_wash_state_and_the_boolean_disagree_on_spelled_out_no_clean():
    """The divergence is the point: only one of these can invert."""
    meta = {"process_clean": "NO CLEAN"}
    assert is_clean_process(meta) is True
    assert wash_state(meta) is WashState.NO_CLEAN
