"""Phase 55 section 8: Standard ingestion preselects the current version too."""

import pathlib

from cockpit.ingestion.locator import PendingSelection, locate
from cockpit.ingestion.roles import SourceRole
from tests.services.phase55_support import PART_NUMBER, job_folder, write


def sets_by_role(outcome) -> dict:
    assert isinstance(outcome, PendingSelection)
    return {candidate_set.role: candidate_set for candidate_set in outcome.sets}


def names(folder: pathlib.Path, *file_names: str) -> None:
    for file_name in file_names:
        write(folder / file_name, "x")


def test_bom_and_eco_candidate_sets_preselect_the_latest_dated(tmp_path):
    folder = job_folder(tmp_path)
    names(
        folder,
        f"{PART_NUMBER} AUDIT BOM.xlsx",
        f"{PART_NUMBER} AUDIT BOM update 10-29-2026.xlsx",
        f"{PART_NUMBER} ECO.docx",
        f"{PART_NUMBER} ECO_10-30-26.docx",
        f"{PART_NUMBER} MFG TRAVELER.xlsx",
    )
    sets = sets_by_role(locate(PART_NUMBER, tmp_path))
    assert sets[SourceRole.BOM].preselected.name == f"{PART_NUMBER} AUDIT BOM update 10-29-2026.xlsx"
    assert sets[SourceRole.NOTES].preselected.name == f"{PART_NUMBER} ECO_10-30-26.docx"
    assert SourceRole.TRAVELER not in sets


def test_the_traveler_rule_is_unchanged_among_undated_names(tmp_path):
    folder = job_folder(tmp_path)
    names(
        folder,
        f"{PART_NUMBER} AUDIT BOM.xlsx",
        f"{PART_NUMBER} ECO.docx",
        f"{PART_NUMBER} MFG TRAVELER rev A.xlsx",
        f"{PART_NUMBER} MFG TRAVELER rev B.xlsx",
    )
    sets = sets_by_role(locate(PART_NUMBER, tmp_path))
    assert sets[SourceRole.TRAVELER].preselected.name == f"{PART_NUMBER} MFG TRAVELER rev B.xlsx"


def test_a_dated_traveler_is_preselected_over_a_revision(tmp_path):
    folder = job_folder(tmp_path)
    names(
        folder,
        f"{PART_NUMBER} AUDIT BOM.xlsx",
        f"{PART_NUMBER} ECO.docx",
        f"{PART_NUMBER} MFG TRAVELER rev C.xlsx",
        f"{PART_NUMBER} MFG TRAVELER_10-30-26.xlsx",
    )
    sets = sets_by_role(locate(PART_NUMBER, tmp_path))
    assert sets[SourceRole.TRAVELER].preselected.name == f"{PART_NUMBER} MFG TRAVELER_10-30-26.xlsx"


def test_a_tie_leaves_the_choice_to_you(tmp_path):
    folder = job_folder(tmp_path)
    names(
        folder,
        f"{PART_NUMBER} AUDIT BOM update 10-29-2026.xlsx",
        f"{PART_NUMBER} AUDIT BOM (Updated 10-29-2026).xlsx",
        f"{PART_NUMBER} ECO.docx",
        f"{PART_NUMBER} ECO copy.docx",
        f"{PART_NUMBER} MFG TRAVELER.xlsx",
    )
    sets = sets_by_role(locate(PART_NUMBER, tmp_path))
    assert sets[SourceRole.BOM].preselected is None
    assert sets[SourceRole.NOTES].preselected is None


def test_the_drawing_is_never_preselected(tmp_path):
    folder = job_folder(tmp_path)
    names(
        folder,
        f"{PART_NUMBER} AUDIT BOM.xlsx",
        f"{PART_NUMBER} ECO.docx",
        f"{PART_NUMBER} MFG TRAVELER.xlsx",
        f"{PART_NUMBER} drawing.pdf",
        f"{PART_NUMBER} drawing update 10-30-2026.pdf",
    )
    sets = sets_by_role(locate(PART_NUMBER, tmp_path))
    assert sets[SourceRole.PDF].preselected is None
