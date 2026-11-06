"""Phase 55 section 3.2 (version tags) and section 3.3's choose_current_version."""

import datetime

import pytest

from cockpit.ingestion.locator import choose_current_version
from cockpit.ingestion.roles import SourceRole
from cockpit.ingestion.version_tags import UNDATED, Dated, tag_rank, version_tag

D = datetime.date


@pytest.mark.parametrize("name, expected", [
    ("B900001 AUDIT BOM update 10-15-2026.xlsx", D(2026, 10, 15)),
    ("B900001 AUDIT BOM (Updated 10-15-2026).xlsx", D(2026, 10, 15)),
    ("B900001 AUDIT BOM update2 10-15-2026.xlsx", D(2026, 10, 15)),
    ("B900001 ECO_Updated 10-29-2026.docx", D(2026, 10, 29)),
    ("B900001 MFG TRAVELER_10-30-26.xlsx", D(2026, 10, 30)),
    ("B900001 ECO 10.30.2026.docx", D(2026, 10, 30)),
    ("B900001 ECO 10.30.26.docx", D(2026, 10, 30)),
    ("B900001 AUDIT BOM 2026-11-04.xlsx", D(2026, 11, 4)),
    ("B900001 AUDIT BOM 2026.11.4.xlsx", D(2026, 11, 4)),
    ("B900001 AUDIT BOM update 1-2-2027.xlsx", D(2027, 1, 2)),
])
def test_every_corpus_and_sample_form_is_dated(name, expected):
    assert version_tag(name) == Dated(expected)


@pytest.mark.parametrize("name", [
    "B900001 AUDIT BOM.xlsx",
    "B900001 AUDIT BOM Updated 5-21.xlsx",
    "B900001 AUDIT BOM update 11-2026.xlsx",
    "B900001 MFG TRAVELER rev 1.2.xlsx",
    "B900001 MFG TRAVELER rev 1.2.3.xlsx",
])
def test_partial_dates_and_revision_numbers_are_undated(name):
    assert version_tag(name) == UNDATED


def test_a_job_number_is_never_read_as_a_date():
    assert version_tag("B900001 AUDIT BOM.xlsx") == UNDATED
    assert version_tag("B900001.10.30 AUDIT BOM.xlsx") == UNDATED
    assert version_tag("B900001-10-30-26 AUDIT BOM.xlsx") == Dated(D(2026, 10, 30))


def test_two_dates_in_one_name_the_later_counts():
    assert version_tag("BOM update 10-15-2026 update2 10-20-2026.xlsx") == Dated(D(2026, 10, 20))
    assert version_tag("BOM update 10-20-2026 update2 10-15-2026.xlsx") == Dated(D(2026, 10, 20))


def test_the_first_group_decides_the_order():
    assert version_tag("BOM 2026-10-30.xlsx") == Dated(D(2026, 10, 30))
    assert version_tag("BOM 10-30-2026.xlsx") == Dated(D(2026, 10, 30))
    assert version_tag("BOM 1.2.26.xlsx") == Dated(D(2026, 1, 2))


@pytest.mark.parametrize("name", [
    "BOM 10-30.2026.xlsx",
    "BOM 13-01-2026.xlsx",
    "BOM 2-31-2026.xlsx",
    "BOM 30-10-2026.xlsx",
    "BOM 26-10-30.xlsx",
    "BOM 110-30-2026.xlsx",
    "BOM 10-30-20267.xlsx",
    "BOM 2026-10-300.xlsx",
])
def test_mixed_separators_touching_digits_and_impossible_dates_are_undated(name):
    assert version_tag(name) == UNDATED


def test_overlapping_candidates_are_all_considered():
    assert version_tag("BOM 1-2-3-2026.xlsx") == Dated(D(2026, 2, 3))


def test_undated_ranks_below_every_dated():
    assert tag_rank(UNDATED) < tag_rank(Dated(D(1900, 1, 1))) < tag_rank(Dated(D(2026, 1, 1)))


# ---------- choose_current_version ----------

def test_no_names_and_the_only_name():
    assert choose_current_version(SourceRole.BOM, []) is None
    assert choose_current_version(SourceRole.BOM, ["X AUDIT BOM.xlsx"]) == "X AUDIT BOM.xlsx"


def test_the_latest_dated_name_is_chosen():
    names = [
        "X AUDIT BOM update 10-15-2026.xlsx",
        "X AUDIT BOM update 10-29-2026.xlsx",
        "X AUDIT BOM.xlsx",
    ]
    assert choose_current_version(SourceRole.BOM, names) == "X AUDIT BOM update 10-29-2026.xlsx"


def test_a_tie_on_the_latest_date_chooses_nothing():
    names = ["X AUDIT BOM update 10-29-2026.xlsx", "X AUDIT BOM (Updated 10-29-2026).xlsx"]
    assert choose_current_version(SourceRole.BOM, names) is None


def test_a_dated_traveler_beats_an_undated_revision():
    names = ["X MFG TRAVELER rev C.xlsx", "X MFG TRAVELER_10-30-26.xlsx"]
    assert choose_current_version(SourceRole.TRAVELER, names) == "X MFG TRAVELER_10-30-26.xlsx"


def test_undated_travelers_rank_by_revision():
    names = ["X MFG TRAVELER rev A.xlsx", "X MFG TRAVELER rev B.xlsx"]
    assert choose_current_version(SourceRole.TRAVELER, names) == "X MFG TRAVELER rev B.xlsx"


def test_a_rank_naming_no_revision_chooses_nothing():
    names = ["X MFG TRAVELER.xlsx", "X MFG TRAVELER copy.xlsx"]
    assert choose_current_version(SourceRole.TRAVELER, names) is None


def test_revision_ranking_is_for_travelers_only():
    names = ["X AUDIT BOM rev A.xlsx", "X AUDIT BOM rev B.xlsx"]
    assert choose_current_version(SourceRole.BOM, names) is None
    assert choose_current_version(SourceRole.NOTES, ["X ECO.docx", "X ECO copy.docx"]) is None
