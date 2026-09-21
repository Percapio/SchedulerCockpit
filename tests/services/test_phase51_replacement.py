"""Phase 51 section 3 — identity pre-flight, list_family and replacement."""

import pytest

from cockpit.ingestion.errors import ProbedIdentityMismatch
from cockpit.ingestion.service import IngestionPlan, ProbedIdentity
from cockpit.persistence.errors import DuplicateIdentityError
from cockpit.persistence.types import SourceFileCategory
from tests.services.test_ingestion_service import ingestion_service  # noqa: F401
from tests.services.test_phase51_ingest_storage import stub_parsers, write_quartet


def ingest(service, tmp_path, name, marker, monkeypatch, part, wo, qty=10, plan=None):
    paths = write_quartet(tmp_path / name, "B123456", marker)
    stub_parsers(monkeypatch, part, wo, quantity=qty)
    return service.ingest(paths, plan=plan)


def probe_for(audit, traveler_hash="deadbeef", quantity=10):
    return ProbedIdentity(
        part_number=audit.part_number, work_order_ref=audit.work_order_ref,
        quantity=quantity, traveler_hash=traveler_hash,
    )


# --- list_family ------------------------------------------------------------

def test_list_family_spans_every_split_suffix(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    audit = ingest(service, tmp_path, "a", b"x", monkeypatch, "B900001", "WO-1")
    service.audit_repo.relabel_suffix(audit.id, "-1")
    service.audit_repo.clone_to_suffix(audit.id, "-2", 4, "split for test")

    family = service.audit_repo.list_family("B900001", "WO-1")

    assert [a.split_suffix for a in family] == ["-1", "-2"]


def test_list_family_is_empty_for_an_unknown_identity(ingestion_service):  # noqa: F811
    service, _id, _tmp = ingestion_service
    assert service.audit_repo.list_family("B999999", "NOPE") == []


def test_a_split_inherits_its_parents_article(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    audit = ingest(
        service, tmp_path, "a", b"x", monkeypatch, "B900001", "WO-1",
        plan=IngestionPlan(article_revision="2nd"),
    )
    sibling = service.audit_repo.clone_to_suffix(audit.id, "-2", 4, "split for test")

    assert sibling.article_revision == "2nd"


# --- ingest() with no plan is unchanged -------------------------------------

def test_no_plan_records_fa(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    audit = ingest(service, tmp_path, "a", b"x", monkeypatch, "B900001", "WO-1")
    assert audit.article_revision == "FA"


def test_no_plan_still_raises_on_a_colliding_identity(ingestion_service, monkeypatch):  # noqa: F811
    """The dead end Phase 51 replaces, preserved for every caller that wants it."""
    service, _id, tmp_path = ingestion_service
    ingest(service, tmp_path, "a", b"x", monkeypatch, "B900001", "WO-1")

    with pytest.raises(DuplicateIdentityError):
        ingest(service, tmp_path, "b", b"y", monkeypatch, "B900001", "WO-1")


# --- replacement ------------------------------------------------------------

def test_replacement_deletes_every_sibling_not_just_the_exact_suffix(
    ingestion_service, monkeypatch  # noqa: F811
):
    """UNIQUE includes split_suffix, so a '' insert succeeds beside -1 and -2."""
    service, _id, tmp_path = ingestion_service
    first = ingest(service, tmp_path, "a", b"x", monkeypatch, "B900001", "WO-1")
    service.audit_repo.relabel_suffix(first.id, "-1")
    service.audit_repo.clone_to_suffix(first.id, "-2", 4, "split for test")
    family = service.audit_repo.list_family("B900001", "WO-1")
    assert len(family) == 2

    replacement = ingest(
        service, tmp_path, "b", b"y", monkeypatch, "B900001", "WO-1",
        plan=IngestionPlan(replace_family=tuple(a.id for a in family), article_revision="2nd"),
    )

    after = service.audit_repo.list_family("B900001", "WO-1")
    assert [a.id for a in after] == [replacement.id]
    assert after[0].article_revision == "2nd"
    assert after[0].split_suffix == ""


def test_standard_replacement_records_fa_even_over_a_second_article(
    ingestion_service, monkeypatch  # noqa: F811
):
    service, _id, tmp_path = ingestion_service
    stored = ingest(
        service, tmp_path, "a", b"x", monkeypatch, "B900001", "WO-1",
        plan=IngestionPlan(article_revision="2nd"),
    )

    replacement = ingest(
        service, tmp_path, "b", b"y", monkeypatch, "B900001", "WO-1",
        plan=IngestionPlan(replace_family=(stored.id,)),
    )

    assert replacement.article_revision == "FA"


def test_the_superseded_families_files_are_reaped(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    stored = ingest(service, tmp_path, "a", b"rev-A-", monkeypatch, "B900001", "WO-1")
    old_paths = {sf.local_storage_path for sf in service.source_file_repo.list_for_audit(stored.id)}

    ingest(
        service, tmp_path, "b", b"rev-B-", monkeypatch, "B900001", "WO-1",
        plan=IngestionPlan(replace_family=(stored.id,)),
    )

    assert not any(p.exists() for p in old_paths)


def test_an_unchanged_role_is_retained_by_reference_count(ingestion_service, monkeypatch):  # noqa: F811
    """The shared root drawing in an article revision is the real case."""
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "a", "B123456", b"same-bytes-")
    stub_parsers(monkeypatch, "B900001", "WO-1")
    stored = service.ingest(paths)
    pdf = next(
        sf for sf in service.source_file_repo.list_for_audit(stored.id)
        if sf.file_category == SourceFileCategory.PDF
    )

    stub_parsers(monkeypatch, "B900001", "WO-1")
    replacement = service.ingest(paths, plan=IngestionPlan(replace_family=(stored.id,)))

    assert pdf.local_storage_path.exists(), "reaped a file the new row references"
    new_pdf = next(
        sf for sf in service.source_file_repo.list_for_audit(replacement.id)
        if sf.file_category == SourceFileCategory.PDF
    )
    assert new_pdf.local_storage_path == pdf.local_storage_path


# --- the identity assertion --------------------------------------------------

def test_a_drifted_identity_aborts_with_the_family_intact(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    stored = ingest(service, tmp_path, "a", b"x", monkeypatch, "B900001", "WO-1")

    # The operator confirmed a replacement of WO-1; the share now says WO-2.
    stale_probe = ProbedIdentity("B900001", "WO-1", 10, "hash-the-operator-saw")
    with pytest.raises(ProbedIdentityMismatch):
        ingest(
            service, tmp_path, "b", b"y", monkeypatch, "B900001", "WO-2",
            plan=IngestionPlan(expected_identity=stale_probe, replace_family=(stored.id,)),
        )

    family = service.audit_repo.list_family("B900001", "WO-1")
    assert [a.id for a in family] == [stored.id]


def test_a_changed_traveler_hash_aborts_too(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    stored = ingest(service, tmp_path, "a", b"x", monkeypatch, "B900001", "WO-1")

    with pytest.raises(ProbedIdentityMismatch):
        ingest(
            service, tmp_path, "b", b"y", monkeypatch, "B900001", "WO-1",
            plan=IngestionPlan(
                expected_identity=ProbedIdentity("B900001", "WO-1", 10, "not-the-real-hash"),
                replace_family=(stored.id,),
            ),
        )

    assert len(service.audit_repo.list_family("B900001", "WO-1")) == 1


# --- quantity override -------------------------------------------------------

def test_the_override_sets_the_column_and_leaves_metadata_alone(
    ingestion_service, monkeypatch  # noqa: F811
):
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "a", "B123456", b"x")
    stub_parsers(monkeypatch, "B900001", "WO-1", quantity=10, metadata={"quantity": 10})

    audit = service.ingest(paths, plan=IngestionPlan(quantity_override=250))

    assert audit.quantity == 250
    assert audit.traveler_metadata["quantity"] == 10, "parsed evidence must survive"


def test_an_override_below_one_is_refused(ingestion_service, monkeypatch):  # noqa: F811
    service, _id, tmp_path = ingestion_service
    paths = write_quartet(tmp_path / "a", "B123456", b"x")
    stub_parsers(monkeypatch, "B900001", "WO-1")

    with pytest.raises(Exception):
        service.ingest(paths, plan=IngestionPlan(quantity_override=0))

    assert service.audit_repo.list_family("B900001", "WO-1") == []
