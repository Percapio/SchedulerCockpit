"""Phase 49 section 8.2, as amended by Patch 10 — the wash designation.

The Audit List reads the traveler's own process_clean wording through
wash_state. Patch 10 made is_clean_process a derivation of that same reading,
so the two can no longer disagree; the invariant is asserted directly in
test_the_boolean_agrees_with_the_designation_for_every_input.

Before Patch 10 the boolean asked only whether the cell was non-empty, so both
Document Control dropdown values read as a wash process and runtime_calc
applied clean_process_multiplier_tht to builds that are never washed.
"""

import logging

import pytest

from cockpit.persistence.traveler_flags import (
    WashState,
    is_clean_process,
    unrecognised_wash_text,
    wash_state,
)
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
        # The two values the Document Control dropdown can produce.
        ({"process_clean": "Clean"}, True),
        ({"process_clean": "No Clean"}, False),
        ({"process_clean": "CLEAN"}, True),
        ({"process_clean": "NO CLEAN"}, False),
        ({"process_clean": ""}, False),
        ({}, False),
        # Whitespace-only strips to blank, which is UNKNOWN, not a wash process.
        ({"process_clean": "   "}, False),
        # Off-template text is UNRECOGNISED and never claims a wash process.
        ({"process_clean": "NC"}, False),
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


def test_unparsed_traveler_is_indistinguishable_from_no_clean_on_the_boolean():
    """The boolean cannot separate "said nothing" from "said no clean".

    Both correctly yield False, because neither is a wash process. The
    distinction the boolean loses is preserved in wash_state, which is why the
    Wash column can render blank and NC differently.
    """
    assert is_clean_process({}) is False
    assert is_clean_process({"process_clean": ""}) is False
    assert is_clean_process({"process_clean": "No Clean"}) is False

    assert wash_state({}) is WashState.UNKNOWN
    assert wash_state({"process_clean": "No Clean"}) is WashState.NO_CLEAN


def test_no_clean_text_is_not_read_as_a_wash_process_by_the_boolean():
    """Patch 10: the multiplier no longer fires on a No Clean build.

    is_clean_process asked only whether process_clean was non-empty, so both
    dropdown values read as a wash process and clean_process_multiplier_tht
    was applied to builds that are never washed. This is the inversion of the
    test Phase 49 left behind for exactly this fix.
    """
    assert is_clean_process({"process_clean": "No Clean"}) is False
    assert is_clean_process({"process_clean": "NO CLEAN"}) is False
    assert is_clean_process({"process_clean": "Clean"}) is True
    assert is_clean_process({"process_clean": "CLEAN"}) is True


# --- wash_state, which the Audit List does use ------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        # The dropdown's own two values, at every casing and separator.
        ("Clean", WashState.CLEAN),
        ("No Clean", WashState.NO_CLEAN),
        ("CLEAN", WashState.CLEAN),
        ("clean", WashState.CLEAN),
        ("NO CLEAN", WashState.NO_CLEAN),
        ("NO-CLEAN", WashState.NO_CLEAN),
        ("NOCLEAN", WashState.NO_CLEAN),
        ("Non-Clean", WashState.NO_CLEAN),
        ("", WashState.UNKNOWN),
        ("   ", WashState.UNKNOWN),
        (None, WashState.UNKNOWN),
        (3, WashState.UNKNOWN),
        # Off-template. Each of these previously read as CLEAN through the
        # fall-through branch, which is what let an unreadable cell claim a
        # wash process. "WASH" and "AQUEOUS WASH" are plausible wordings the
        # dropdown cannot produce, and Patch 10 section 6.2 declines to guess
        # at them: they surface in the log instead.
        ("WASH", WashState.UNRECOGNISED),
        ("AQUEOUS WASH", WashState.UNRECOGNISED),
        ("NC", WashState.UNRECOGNISED),
        ("C", WashState.UNRECOGNISED),
    ],
)
def test_wash_state_reads_the_travelers_wording(text, expected):
    assert wash_state({"process_clean": text}) is expected


def test_wash_state_of_an_unmapped_traveler_is_unknown():
    """The state every audit ingested before 2026-06-06 is actually in."""
    assert wash_state({}) is WashState.UNKNOWN
    assert wash_state({"process": "LEAD FREE"}) is WashState.UNKNOWN
    assert wash_state(None) is WashState.UNKNOWN


@pytest.mark.parametrize(
    "metadata",
    [
        {"process_clean": "Clean"},
        {"process_clean": "No Clean"},
        {"process_clean": "CLEAN"},
        {"process_clean": "NO CLEAN"},
        {"process_clean": "no-clean"},
        {"process_clean": "NC"},
        {"process_clean": "WASH"},
        {"process_clean": ""},
        {"process_clean": "   "},
        {"process_clean": None},
        {"process_clean": 3},
        {},
        {"process": "LEAD FREE"},
        None,
        "not a mapping",
    ],
)
def test_the_boolean_agrees_with_the_designation_for_every_input(metadata):
    """The invariant that stops the two readings diverging a second time.

    Phase 49 left wash_state and is_clean_process as independent readings of
    one cell and they disagreed on No Clean. There is now one reading, and
    this asserts it stays that way for every shape the cell can take.
    """
    assert is_clean_process(metadata) == (wash_state(metadata) is WashState.CLEAN)


# --- Patch 10: the off-template traveler -----------------------------------

def test_unrecognised_wash_text_names_only_the_off_template_case():
    assert unrecognised_wash_text({"process_clean": "NC"}) == "NC"
    assert unrecognised_wash_text({"process_clean": "  WASH  "}) == "WASH"
    assert unrecognised_wash_text({"process_clean": "Clean"}) is None
    assert unrecognised_wash_text({"process_clean": "No Clean"}) is None
    assert unrecognised_wash_text({}) is None
    assert unrecognised_wash_text(None) is None


def test_an_off_template_traveler_warns_once_at_the_write_boundary(
    ingestion_service, monkeypatch, caplog  # noqa: F811
):
    """Section 6.1: the log lives at the write path, not in the predicate.

    wash_state runs on every Audit List repaint, so logging inside it would
    either flood or need a cross-thread memo of already-logged spellings.
    """
    service, _, tmp_path = ingestion_service

    with caplog.at_level(logging.WARNING, logger="cockpit.persistence.traveler_flags"):
        audit = _ingest_with_metadata(
            service, tmp_path, monkeypatch, {"process_clean": "NC"}
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "NC" in warnings[0].getMessage()

    stored = service.audit_repo.find_by_id(audit.id)
    assert stored.is_clean_process is False


def test_repainting_the_column_never_logs(caplog):
    """The predicate is pure. Only a write says anything."""
    with caplog.at_level(logging.WARNING, logger="cockpit.persistence.traveler_flags"):
        for _ in range(50):
            wash_state({"process_clean": "NC"})
            is_clean_process({"process_clean": "NC"})
    assert caplog.records == []


def test_a_dropdown_value_writes_no_warning(
    ingestion_service, monkeypatch, caplog  # noqa: F811
):
    service, _, tmp_path = ingestion_service
    with caplog.at_level(logging.WARNING, logger="cockpit.persistence.traveler_flags"):
        _ingest_with_metadata(
            service, tmp_path, monkeypatch, {"process_clean": "No Clean"}
        )
    assert caplog.records == []
