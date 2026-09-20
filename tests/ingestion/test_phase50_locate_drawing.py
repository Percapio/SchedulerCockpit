"""Phase 50 section 4.2 — the PDF-only locate."""

import pytest

from cockpit.ingestion.errors import (
    JobDirectoryEscapesRoot,
    JobDirectoryNotFound,
    JobNumberMalformed,
    RequiredRoleMissing,
    SourceRootUnreachable,
)
from cockpit.ingestion.locator import (
    LocatedDrawing,
    PendingDrawingSelection,
    as_pending_selection,
    locate_drawing,
)
from cockpit.ingestion.roles import SourceRole


@pytest.fixture
def job_dir(tmp_path):
    """<root>/B123xxx/B1234xx/B123456, matching resolve_job_directory."""
    root = tmp_path / "share"
    d = root / "B123xxx" / "B1234xx" / "B123456"
    d.mkdir(parents=True)
    return root, d


def test_single_pdf_resolves(job_dir):
    root, d = job_dir
    (d / "B123456_SMT.pdf").write_bytes(b"x")

    outcome = locate_drawing("B123456", root)

    assert isinstance(outcome, LocatedDrawing)
    assert outcome.pdf_path.name == "B123456_SMT.pdf"
    assert outcome.job_number == "B123456"


def test_several_pdfs_return_a_pending_selection(job_dir):
    root, d = job_dir
    for name in ("b.pdf", "a.pdf", "c.pdf"):
        (d / name).write_bytes(b"x")

    outcome = locate_drawing("B123456", root)

    assert isinstance(outcome, PendingDrawingSelection)
    assert [p.name for p in outcome.candidates] == ["a.pdf", "b.pdf", "c.pdf"]


def test_no_pdf_names_pdf_alone_and_lists_what_was_present(job_dir):
    root, d = job_dir
    (d / "B123456 AUDIT BOM.xlsx").write_bytes(b"x")
    (d / "B123456 ECO.docx").write_bytes(b"x")

    with pytest.raises(RequiredRoleMissing) as excinfo:
        locate_drawing("B123456", root)

    assert excinfo.value.missing_roles == ["PDF"]
    assert set(excinfo.value.present_file_names) == {
        "B123456 AUDIT BOM.xlsx",
        "B123456 ECO.docx",
    }


def test_succeeds_where_locate_cannot(job_dir):
    """The case that justifies the function existing at all.

    A folder with a BOM and a drawing but no traveler: `locate` raises
    RequiredRoleMissing before the PDF is ever considered.
    """
    from cockpit.ingestion.locator import locate

    root, d = job_dir
    (d / "B123456 AUDIT BOM.xlsx").write_bytes(b"x")
    (d / "B123456_SMT.pdf").write_bytes(b"x")

    with pytest.raises(RequiredRoleMissing):
        locate("B123456", root)

    assert isinstance(locate_drawing("B123456", root), LocatedDrawing)


def test_subdirectories_are_not_searched(job_dir):
    root, d = job_dir
    nested = d / "Archive"
    nested.mkdir()
    (nested / "superseded.pdf").write_bytes(b"x")

    with pytest.raises(RequiredRoleMissing):
        locate_drawing("B123456", root)


def test_malformed_job_number(tmp_path):
    with pytest.raises(JobNumberMalformed):
        locate_drawing("nope", tmp_path)


def test_unreachable_root(tmp_path):
    with pytest.raises(SourceRootUnreachable):
        locate_drawing("B123456", tmp_path / "missing")


def test_job_directory_not_found(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    with pytest.raises(JobDirectoryNotFound):
        locate_drawing("B123456", root)


def test_job_directory_escaping_the_root_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "share"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "B123456"
    outside.mkdir(parents=True)
    (outside / "B123456_SMT.pdf").write_bytes(b"x")

    monkeypatch.setattr(
        "cockpit.ingestion.locator.resolve_job_directory",
        lambda job_number, source_root: outside,
    )

    with pytest.raises(JobDirectoryEscapesRoot):
        locate_drawing("B123456", root)


def test_adapter_produces_one_pdf_candidate_set(job_dir):
    root, d = job_dir
    for name in ("a.pdf", "b.pdf"):
        (d / name).write_bytes(b"x")

    pending = as_pending_selection(locate_drawing("B123456", root))

    assert len(pending.sets) == 1
    assert pending.sets[0].role == SourceRole.PDF
    assert pending.sets[0].preselected is None
    assert pending.resolved == {}
    assert len(pending.sets[0].candidates) == 2
