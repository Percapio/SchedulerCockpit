"""Phase 54 section 7.1: the requirement scan at ingest."""

from cockpit.services.requirements import DEFAULT_VOCABULARY, RequirementKind, RequirementState
from cockpit.services.requirements_reconciler import stale_scan_jobs
from tests.services.test_ingestion_service import (  # noqa: F401
    DummyLayoutParser, ParserRegistry, ingestion_service,
)
from tests.services.test_phase51_ingest_storage import stub_parsers, write_quartet


def test_an_ingest_without_a_vocabulary_writes_no_scan(ingestion_service, monkeypatch):  # noqa: F811
    """Every caller from before Phase 54 passes none, and behaves as it did; the reconciler fills the row."""
    service, _existing_id, tmp_path = ingestion_service
    stub_parsers(monkeypatch, "B900001", "WO-A")
    audit = service.ingest(write_quartet(tmp_path / "job", "B900001", b"revision-A-"))
    assert service.requirements_repo.find_summary(audit.id) is None


def test_an_ingest_with_a_vocabulary_writes_a_scan_the_reconciler_agrees_with(ingestion_service, monkeypatch):  # noqa: F811
    service, _existing_id, tmp_path = ingestion_service
    stub_parsers(monkeypatch, "B900001", "WO-A")
    audit = service.ingest(
        write_quartet(tmp_path / "job", "B900001", b"revision-A-"),
        requirements_vocabulary=DEFAULT_VOCABULARY,
    )

    summary = service.requirements_repo.find_summary(audit.id)
    assert summary is not None
    # The stand-in files aren't real documents: unreadable, never a failed ingest.
    assert summary.states[RequirementKind.XRAY_SAVE_IMAGES] == RequirementState.UNKNOWN
    assert summary.unknown_reasons[RequirementKind.XRAY_SAVE_IMAGES]
    # Same inputs, same fingerprint: the reconciler doesn't rescan a fresh ingest.
    assert audit.id not in {job.audit_id for job in stale_scan_jobs(service.conn, DEFAULT_VOCABULARY)}
