"""Phase 51 sections 2.1-2.3 — content-addressed ingest storage.

The first test is the section 8.1 characterization: two jobs whose BOM, traveler
and ECO share filenames but differ in content. Against the unrepaired ingest()
the second job is parsed from the first's stored files and its rows carry hashes
that do not describe the bytes on disk.
"""

import pathlib
import sqlite3

import pytest

from cockpit.ingestion import hashing
from cockpit.persistence.types import ActiveAuditDraft, SourceFileCategory
from tests.services.test_ingestion_service import (  # noqa: F401
    DummyLayoutParser, ParserRegistry, ingestion_service,
)


def write_quartet(root: pathlib.Path, job: str, marker: bytes, with_pdf=True):
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "bom": root / f"{job} AUDIT BOM.xlsx",
        "traveler": root / f"{job} MFG Traveler.xlsx",
        "notes": root / f"{job} ECO.docx",
    }
    for path in paths.values():
        path.write_bytes(marker + path.name.encode())
    if with_pdf:
        paths["pdf"] = root / f"{job}_SMT.pdf"
        paths["pdf"].write_bytes(marker + b"pdf")
    return list(paths.values())


def stub_parsers(monkeypatch, part_number: str, work_order: str, quantity: int = 1,
                 metadata=None):
    from cockpit.ingestion.parsers.results import BomItem, IngestionIntent

    monkeypatch.setattr("cockpit.ingestion.parsers.audit_bom.parse", lambda p: None)
    monkeypatch.setattr(
        "cockpit.ingestion.parsers.eco_build_notes.parse",
        lambda p: type("EcoResult", (), {"declared_part_number": "", "row_count": 1,
                                         "raw_table_count": 1})(),
    )
    monkeypatch.setattr("cockpit.ingestion.parsers.traveler.parse", lambda p, cm: None)

    intent = IngestionIntent(
        audit_draft=ActiveAuditDraft(
            part_number=part_number, work_order_ref=work_order, quantity=quantity,
            traveler_metadata=metadata or {},
        ),
        bom_items=[BomItem(component_mpn="C-1", description="d", mount_type="S",
                           ref_des_raw="C1", ref_des_list=("C1",), find_number="1")],
    )
    monkeypatch.setattr("cockpit.ingestion.cross_validation.reconcile",
                        lambda b, e, t, cm: intent)
    return intent


def rows_for(service, audit_id):
    return {sf.file_category: sf for sf in service.source_file_repo.list_for_audit(audit_id)}


# --- section 8.1, the characterization --------------------------------------

def test_every_registered_hash_matches_its_stored_bytes(ingestion_service, monkeypatch):  # noqa: F811
    """Two jobs, same filenames, different content.

    Under the unrepaired ingest() the second job's copies are all skipped and
    its rows carry hashes describing bytes that are not on disk — which is what
    makes the startup sweep unlink the files the audit points at.
    """
    service, _existing_id, tmp_path = ingestion_service

    first = write_quartet(tmp_path / "jobA", "B123456", b"revision-A-")
    stub_parsers(monkeypatch, "B900001", "WO-A")
    audit_a = service.ingest(first)

    second = write_quartet(tmp_path / "jobB", "B123456", b"revision-B-materially-other-")
    stub_parsers(monkeypatch, "B900002", "WO-B")
    audit_b = service.ingest(second)

    for audit_id in (audit_a.id, audit_b.id):
        for sf in service.source_file_repo.list_for_audit(audit_id):
            assert sf.local_storage_path.exists(), sf.file_category
            assert hashing.sha256_hex(sf.local_storage_path) == sf.file_hash, sf.file_category


# --- section 8.2, storage ---------------------------------------------------

def test_all_four_roles_are_content_addressed(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "job", "B123456", b"x")
    stub_parsers(monkeypatch, "B900001", "WO-A")

    audit = service.ingest(paths)

    stored = rows_for(service, audit.id)
    assert set(stored) == {
        SourceFileCategory.BOM, SourceFileCategory.TRAVELER,
        SourceFileCategory.NOTES, SourceFileCategory.PDF,
    }
    for category, sf in stored.items():
        assert "__" in sf.local_storage_path.name, category
        assert sf.file_hash[:12] in sf.local_storage_path.name, category


def test_original_filename_never_carries_the_hash_suffix(ingestion_service, monkeypatch):  # noqa: F811
    """It is what the UI displays; the mangled on-disk name must not reach it."""
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "job", "B123456", b"x")
    stub_parsers(monkeypatch, "B900001", "WO-A")

    audit = service.ingest(paths)

    names = {sf.original_filename for sf in service.source_file_repo.list_for_audit(audit.id)}
    assert names == {
        "B123456 AUDIT BOM.xlsx", "B123456 MFG Traveler.xlsx",
        "B123456 ECO.docx", "B123456_SMT.pdf",
    }
    assert not any("__" in n for n in names)


def test_same_named_revisions_land_at_distinct_paths(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    stub_parsers(monkeypatch, "B900001", "WO-A")
    a = service.ingest(write_quartet(tmp_path / "a", "B123456", b"rev-A-"))
    stub_parsers(monkeypatch, "B900002", "WO-B")
    b = service.ingest(write_quartet(tmp_path / "b", "B123456", b"rev-B-"))

    paths_a = {sf.local_storage_path for sf in service.source_file_repo.list_for_audit(a.id)}
    paths_b = {sf.local_storage_path for sf in service.source_file_repo.list_for_audit(b.id)}
    assert not paths_a & paths_b
    assert all(p.exists() for p in paths_a | paths_b)


def test_byte_identical_reingest_reuses_the_paths(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "job", "B123456", b"x")
    stub_parsers(monkeypatch, "B900001", "WO-A")
    a = service.ingest(paths)
    stub_parsers(monkeypatch, "B900002", "WO-B")
    b = service.ingest(paths)

    paths_a = {sf.local_storage_path for sf in service.source_file_repo.list_for_audit(a.id)}
    paths_b = {sf.local_storage_path for sf in service.source_file_repo.list_for_audit(b.id)}
    assert paths_a == paths_b
    assert all(p.exists() for p in paths_a)


def test_ingest_without_a_pdf_still_works(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "job", "B123456", b"x", with_pdf=False)
    stub_parsers(monkeypatch, "B900001", "WO-A")

    audit = service.ingest(paths)

    stored = rows_for(service, audit.id)
    assert SourceFileCategory.PDF not in stored
    assert len(stored) == 3


# --- section 2.3, verification and the rollback guard -----------------------

def test_foreign_bytes_at_a_destination_abort_before_any_row(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "job", "B123456", b"x")
    bom = next(p for p in paths if "AUDIT BOM" in p.name)

    audit_dir = service.file_storage_root / "B123456" / "unsplit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    decoy = audit_dir / f"{bom.stem}__{hashing.sha256_hex(bom)[:12]}{bom.suffix}"
    decoy.write_bytes(b"foreign bytes that are not the workbook")

    stub_parsers(monkeypatch, "B900001", "WO-A")
    with pytest.raises(Exception):
        service.ingest(paths)

    # This call did not create the decoy, so it must not delete it.
    assert decoy.exists()
    assert decoy.read_bytes() == b"foreign bytes that are not the workbook"


def test_a_failed_ingest_keeps_files_another_audit_references(ingestion_service, monkeypatch):  # noqa: F811
    """Phase 45 section 4.2's guard, which ingest() did not previously carry."""
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "job", "B123456", b"x")
    stub_parsers(monkeypatch, "B900001", "WO-A")
    first = service.ingest(paths)
    kept = {sf.local_storage_path for sf in service.source_file_repo.list_for_audit(first.id)}

    # The same bytes re-dropped, but the parse blows up after the copy step.
    stub_parsers(monkeypatch, "B900002", "WO-B")
    monkeypatch.setattr(
        "cockpit.ingestion.parsers.audit_bom.parse",
        lambda p: (_ for _ in ()).throw(RuntimeError("parser exploded")),
    )
    with pytest.raises(RuntimeError):
        service.ingest(paths)

    assert all(p.exists() for p in kept), "unlinked a file the first audit still references"
