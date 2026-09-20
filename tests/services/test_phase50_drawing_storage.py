"""Phase 50 sections 3.1-3.4 — the drawing replace defect and its repair.

The first test is the characterization from section 7.1. It asserts the
registered file_hash equals the hash of the bytes actually on disk, which is
precisely what Phase 45 section 6.1 says is violated today. It fails against the
unrepaired add_pdf_to_audit and passes after.
"""

import sqlite3

import pytest

from cockpit.ingestion import hashing
from cockpit.persistence.types import SourceFileCategory
from tests.services.test_ingestion_service import ingestion_service  # noqa: F401


def stored_pdf_row(service, audit_id):
    return service.source_file_repo.find_by_audit_and_category(
        audit_id, SourceFileCategory.PDF
    )


# --- section 7.1, the characterization --------------------------------------

def test_registered_hash_matches_the_bytes_on_disk_after_a_same_named_replace(
    ingestion_service,  # noqa: F811
):
    """Phase 45 section 6.1, stated as an assertion.

    A revision re-exported under the same filename must not leave a row whose
    file_hash describes bytes the stored file does not contain. If it does, the
    startup sweep computes the old hash, finds nothing referencing it, and
    unlinks the file the audit points at.
    """
    service, audit_id, tmp_path = ingestion_service

    original = tmp_path / "B123456_SMT.pdf"
    original.write_bytes(b"%PDF-1.4 revision A")
    service.add_pdf_to_audit(audit_id, original)

    revised = tmp_path / "revised" / "B123456_SMT.pdf"
    revised.parent.mkdir()
    revised.write_bytes(b"%PDF-1.4 revision B, materially different")
    service.add_pdf_to_audit(audit_id, revised)

    row = stored_pdf_row(service, audit_id)
    assert row is not None
    assert row.local_storage_path.exists()
    assert hashing.sha256_hex(row.local_storage_path) == row.file_hash
    assert row.local_storage_path.read_bytes() == revised.read_bytes()


# --- section 7.2, storage and replace ---------------------------------------

def test_same_named_revisions_get_distinct_stored_paths(ingestion_service):  # noqa: F811
    service, audit_id, tmp_path = ingestion_service

    original = tmp_path / "B123456_SMT.pdf"
    original.write_bytes(b"revision A")
    service.add_pdf_to_audit(audit_id, original)
    first_path = stored_pdf_row(service, audit_id).local_storage_path

    revised = tmp_path / "revised" / "B123456_SMT.pdf"
    revised.parent.mkdir()
    revised.write_bytes(b"revision B")
    service.add_pdf_to_audit(audit_id, revised)
    second_path = stored_pdf_row(service, audit_id).local_storage_path

    assert first_path != second_path


def test_superseded_file_is_reaped(ingestion_service):  # noqa: F811
    service, audit_id, tmp_path = ingestion_service

    original = tmp_path / "B123456_SMT.pdf"
    original.write_bytes(b"revision A")
    service.add_pdf_to_audit(audit_id, original)
    first_path = stored_pdf_row(service, audit_id).local_storage_path

    revised = tmp_path / "revised" / "B123456_SMT.pdf"
    revised.parent.mkdir()
    revised.write_bytes(b"revision B")
    service.add_pdf_to_audit(audit_id, revised)

    assert not first_path.exists()
    assert stored_pdf_row(service, audit_id).local_storage_path.exists()


def test_original_filename_is_preserved_for_display(ingestion_service):  # noqa: F811
    service, audit_id, tmp_path = ingestion_service

    original = tmp_path / "B123456_SMT.pdf"
    original.write_bytes(b"revision A")
    service.add_pdf_to_audit(audit_id, original)

    row = stored_pdf_row(service, audit_id)
    assert row.original_filename == "B123456_SMT.pdf"
    assert "__" in row.local_storage_path.name


def test_byte_identical_redrop_is_a_no_op(ingestion_service):  # noqa: F811
    service, audit_id, tmp_path = ingestion_service

    original = tmp_path / "B123456_SMT.pdf"
    original.write_bytes(b"revision A")
    service.add_pdf_to_audit(audit_id, original)
    first = stored_pdf_row(service, audit_id)

    service.add_pdf_to_audit(audit_id, original)
    second = stored_pdf_row(service, audit_id)

    assert first.local_storage_path == second.local_storage_path
    assert second.local_storage_path.exists()
    assert hashing.sha256_hex(second.local_storage_path) == second.file_hash


def test_replacing_twice_leaves_exactly_one_live_file(ingestion_service):  # noqa: F811
    service, audit_id, tmp_path = ingestion_service

    for index, payload in enumerate((b"rev A", b"rev B", b"rev C")):
        source = tmp_path / f"src{index}" / "B123456_SMT.pdf"
        source.parent.mkdir()
        source.write_bytes(payload)
        service.add_pdf_to_audit(audit_id, source)

    row = stored_pdf_row(service, audit_id)
    audit_dir = row.local_storage_path.parent
    assert [p.name for p in audit_dir.iterdir()] == [row.local_storage_path.name]


def test_no_orphaned_coords_survive_a_replace(ingestion_service):  # noqa: F811
    """Asserted as "no coord points at a dead row" rather than by captured id.

    source_files.id is a rowid alias, so SQLite reuses the deleted row's id for
    the replacement and a by-id assertion would be measuring the new row.
    """
    service, audit_id, tmp_path = ingestion_service

    original = tmp_path / "B123456_SMT.pdf"
    original.write_bytes(b"revision A")
    service.add_pdf_to_audit(audit_id, original)

    revised = tmp_path / "revised" / "B123456_SMT.pdf"
    revised.parent.mkdir()
    revised.write_bytes(b"revision B")
    service.add_pdf_to_audit(audit_id, revised)

    orphans = service.conn.execute(
        """
        SELECT COUNT(*) AS n FROM pdf_component_coords c
        WHERE NOT EXISTS (SELECT 1 FROM source_files f WHERE f.id = c.source_file_id)
        """
    ).fetchone()["n"]
    assert orphans == 0

    live_rows = service.conn.execute(
        "SELECT COUNT(DISTINCT source_file_id) AS n FROM pdf_component_coords"
    ).fetchone()["n"]
    assert live_rows <= 1


# --- section 3.3, post-copy verification ------------------------------------

def test_foreign_bytes_at_the_destination_abort_the_operation(
    ingestion_service, monkeypatch  # noqa: F811
):
    """The section 3.3 case: the path exists holding bytes that are not ours.

    Forced by making the destination predictable and planting a decoy there. A
    prefix collision would reach the same state, as would a copy that died
    part-written.
    """
    service, audit_id, tmp_path = ingestion_service

    source = tmp_path / "B123456_SMT.pdf"
    source.write_bytes(b"the real drawing")

    audit_dir = service.file_storage_root / "B123456" / "unsplit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    pdf_hash = hashing.sha256_hex(source)
    decoy = audit_dir / f"B123456_SMT__{pdf_hash[:12]}.pdf"
    decoy.write_bytes(b"foreign bytes that are not the drawing")

    with pytest.raises(Exception):
        service.add_pdf_to_audit(audit_id, source)

    assert stored_pdf_row(service, audit_id) is None
    # This call did not create the decoy, so it must not delete it.
    assert decoy.exists()
    assert decoy.read_bytes() == b"foreign bytes that are not the drawing"


def test_truncated_destination_aborts_and_leaves_the_prior_row_intact(
    ingestion_service,  # noqa: F811
):
    service, audit_id, tmp_path = ingestion_service

    original = tmp_path / "B123456_SMT.pdf"
    original.write_bytes(b"revision A")
    service.add_pdf_to_audit(audit_id, original)
    good_row = stored_pdf_row(service, audit_id)

    revised = tmp_path / "revised" / "B123456_SMT.pdf"
    revised.parent.mkdir()
    revised.write_bytes(b"revision B, longer than the truncation")

    audit_dir = good_row.local_storage_path.parent
    revised_hash = hashing.sha256_hex(revised)
    (audit_dir / f"B123456_SMT__{revised_hash[:12]}.pdf").write_bytes(b"trunc")

    with pytest.raises(Exception):
        service.add_pdf_to_audit(audit_id, revised)

    still = stored_pdf_row(service, audit_id)
    assert still.id == good_row.id
    assert still.local_storage_path.exists()
    assert hashing.sha256_hex(still.local_storage_path) == still.file_hash


def test_parse_failure_leaves_the_prior_drawing_untouched(
    ingestion_service, monkeypatch  # noqa: F811
):
    service, audit_id, tmp_path = ingestion_service

    original = tmp_path / "B123456_SMT.pdf"
    original.write_bytes(b"revision A")
    service.add_pdf_to_audit(audit_id, original)
    good_row = stored_pdf_row(service, audit_id)

    def boom(*args, **kwargs):
        raise RuntimeError("layout parser exploded")

    monkeypatch.setattr(service.layout_parser, "parse", boom)

    revised = tmp_path / "revised" / "B123456_SMT.pdf"
    revised.parent.mkdir()
    revised.write_bytes(b"revision B")

    with pytest.raises(RuntimeError):
        service.add_pdf_to_audit(audit_id, revised)

    still = stored_pdf_row(service, audit_id)
    assert still.id == good_row.id
    assert still.local_storage_path.exists()
    revised_hash = hashing.sha256_hex(revised)
    assert not (
        good_row.local_storage_path.parent / f"B123456_SMT__{revised_hash[:12]}.pdf"
    ).exists()
