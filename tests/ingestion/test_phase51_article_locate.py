"""Phase 51 sections 4.1-4.2 — scope-aware locate and the folder grammar."""

import pytest

from cockpit.ingestion.errors import (
    JobDirectoryEscapesRoot, JobDirectoryNotFound, RequiredRoleMissing,
)
from cockpit.ingestion.filename_rules import article_designation
from cockpit.ingestion.locator import (
    IngestionScope, LocatedFiles, PendingSelection, locate,
)
from cockpit.ingestion.roles import SourceRole


@pytest.fixture
def job(tmp_path):
    root = tmp_path / "share"
    d = root / "B123xxx" / "B1234xx" / "B123456"
    d.mkdir(parents=True)
    return root, d


def trio(folder, job_number="B123456"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{job_number} AUDIT BOM.xlsx").write_bytes(b"b")
    (folder / f"{job_number} MFG Traveler.xlsx").write_bytes(b"t")
    (folder / f"{job_number} ECO.docx").write_bytes(b"e")


# --- the grammar ------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("2nd", "2nd"), ("3RD Article", "3rd"), ("4TH_REV_B", "4th"),
        ("2th", "2th"), ("10th", "10th"),
        ("Archive", None), ("2", None), ("nd2", None), ("", None),
    ],
)
def test_article_designation(name, expected):
    assert article_designation(name) == expected


# --- ROOT scope is unchanged ------------------------------------------------

def test_root_scope_is_todays_behaviour(job):
    root, d = job
    trio(d)
    (d / "B123456_SMT.pdf").write_bytes(b"p")

    outcome = locate("B123456", root)

    assert isinstance(outcome, LocatedFiles)
    assert outcome.job_directory == d
    assert outcome.quartet.pdf_path.name == "B123456_SMT.pdf"


# --- ARTICLE_SUBFOLDER ------------------------------------------------------

def test_trio_comes_from_the_subfolder_and_the_drawing_from_the_root(job):
    root, d = job
    (d / "B123456_SMT.pdf").write_bytes(b"p")
    trio(d / "2nd")

    outcome = locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER)

    assert isinstance(outcome, LocatedFiles)
    assert outcome.quartet.bom_path.parent.name == "2nd"
    assert outcome.quartet.traveler_path.parent.name == "2nd"
    assert outcome.quartet.notes_path.parent.name == "2nd"
    assert outcome.quartet.pdf_path.parent == d


def test_no_matching_subfolder_names_the_required_roles(job):
    root, d = job
    trio(d)
    (d / "Archive").mkdir()

    with pytest.raises(RequiredRoleMissing) as excinfo:
        locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER)

    assert excinfo.value.missing_roles == ["BOM", "TRAVELER", "NOTES"]
    assert "Archive" in excinfo.value.present_file_names


def test_several_subfolders_return_a_folder_set_alone(job):
    root, d = job
    trio(d / "2nd")
    trio(d / "3rd")

    outcome = locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER)

    assert isinstance(outcome, PendingSelection)
    assert len(outcome.sets) == 1, "roles are not knowable before the folder is chosen"
    assert outcome.sets[0].role == SourceRole.ARTICLE_FOLDER
    assert [p.name for p in outcome.sets[0].candidates] == ["2nd", "3rd"]
    assert outcome.resolved == {}


def test_passing_the_folder_back_resolves_the_roles(job):
    root, d = job
    (d / "B123456_SMT.pdf").write_bytes(b"p")
    trio(d / "2nd")
    trio(d / "3rd")

    pending = locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER)
    chosen = next(p for p in pending.sets[0].candidates if p.name == "3rd")

    outcome = locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER, article_folder=chosen)

    assert isinstance(outcome, LocatedFiles)
    assert outcome.quartet.bom_path.parent.name == "3rd"


def test_a_folder_from_a_dialog_still_gets_a_containment_check(job, tmp_path):
    """It arrived from a dialog; that is not a reason to skip the check."""
    root, d = job
    outside = tmp_path / "elsewhere" / "2nd"
    trio(outside)

    with pytest.raises(JobDirectoryEscapesRoot):
        locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER, article_folder=outside)


def test_a_nonexistent_folder_from_a_dialog_is_refused(job):
    root, d = job
    with pytest.raises(JobDirectoryNotFound):
        locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER, article_folder=d / "9th")


def test_only_one_level_is_searched(job):
    """A 3rd folder nested inside a 2nd folder is not found."""
    root, d = job
    trio(d / "2nd" / "3rd")
    (d / "2nd").mkdir(exist_ok=True)

    with pytest.raises(RequiredRoleMissing):
        locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER)


def test_a_drawing_inside_the_subfolder_is_ignored(job):
    """The drawing role always resolves from the root."""
    root, d = job
    (d / "B123456_SMT.pdf").write_bytes(b"root-pdf")
    trio(d / "2nd")
    (d / "2nd" / "B123456_OTHER.pdf").write_bytes(b"subfolder-pdf")

    outcome = locate("B123456", root, IngestionScope.ARTICLE_SUBFOLDER)

    assert isinstance(outcome, LocatedFiles)
    assert outcome.quartet.pdf_path.read_bytes() == b"root-pdf"
