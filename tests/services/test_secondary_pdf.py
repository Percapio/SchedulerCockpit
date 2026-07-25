import pytest
import sqlite3
import pathlib
from cockpit.ingestion.service import IngestionService
from cockpit.persistence.connection import hydrating_row_factory
from cockpit.persistence.schema import migrate
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.notes_checklist import BuildNotesChecklistRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.protocols import ParserRegistry
from cockpit.persistence.types import ActiveAuditDraft, SourceFileCategory
from cockpit.services.layout_query import LayoutQueryService
from cockpit.services.checklist import ChecklistService
from cockpit.services.audit_read import AuditReadService


class DummyLayoutParser:
    def parse(self, pdf_path: pathlib.Path, expected_ref_des: set[str]):
        return None


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "test_sec_pdf.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory

    class DummyParser:
        def parse(self, path): return None

    registry = ParserRegistry(DummyParser(), DummyParser(), DummyParser(), DummyLayoutParser(), None)
    migrate(conn, registry)

    bom_repo = AuditBomComponentRepository(conn)
    pdf_repo = PdfComponentCoordRepository(conn)
    audit_repo = AuditRepository(conn, bom_repo, pdf_repo)
    source_file_repo = SourceFileRepository(conn)
    tht_repo = ThtChecklistRepository(conn)
    notes_repo = BuildNotesChecklistRepository(conn)

    file_storage_root = tmp_path / "cockpit_data"

    service = IngestionService(
        conn=conn,
        audit_repo=audit_repo,
        source_file_repo=source_file_repo,
        tht_repo=tht_repo,
        notes_repo=notes_repo,
        bom_component_repo=bom_repo,
        pdf_coord_repo=pdf_repo,
        layout_parser=DummyLayoutParser(),
        coord_map=None,
        file_storage_root=file_storage_root,
    )

    query_svc = LayoutQueryService(
        source_file_repo=source_file_repo,
        pdf_renderer=None,
        bom_component_repo=bom_repo,
        pdf_coord_repo=pdf_repo,
    )

    checklist_svc = ChecklistService(
        conn=conn,
        audit_repo=audit_repo,
        tht_repo=tht_repo,
        notes_repo=notes_repo,
        source_file_repo=source_file_repo,
        bom_component_repo=bom_repo,
    )

    read_svc = AuditReadService(audit_repo=audit_repo)

    # Register dummy audit
    draft = ActiveAuditDraft(
        part_number="PN-SEC-01",
        work_order_ref="WO-SEC-01",
        split_suffix="",
        quantity=25,
    )
    audit = audit_repo.create(draft)

    return conn, service, query_svc, checklist_svc, read_svc, audit, tmp_path


def test_secondary_pdf_ingestion_and_replacement(env):
    conn, service, query_svc, checklist_svc, read_svc, audit, tmp_path = env

    # Initially no secondary pdf
    assert query_svc.resolve_secondary_pdf_ref(audit.id) is None
    view = checklist_svc.load_active_audit(audit.id)
    assert view.has_secondary_pdf is False

    # Create dummy pdf file 1
    pdf1 = tmp_path / "drawing_v1.pdf"
    pdf1.write_bytes(b"%PDF-1.4 dummy secondary content 1")

    service.add_secondary_pdf_to_audit(audit.id, pdf1)

    # Check query service resolution
    pending = query_svc.resolve_secondary_pdf_ref(audit.id)
    assert pending is not None
    assert pending.path.exists()
    assert pending.path.read_bytes() == b"%PDF-1.4 dummy secondary content 1"

    # Check checklist view
    view = checklist_svc.load_active_audit(audit.id)
    assert view.has_secondary_pdf is True

    # Replace with dummy pdf file 2
    pdf2 = tmp_path / "drawing_v2.pdf"
    pdf2.write_bytes(b"%PDF-1.4 dummy secondary content 2 - updated")

    service.add_secondary_pdf_to_audit(audit.id, pdf2)

    pending2 = query_svc.resolve_secondary_pdf_ref(audit.id)
    assert pending2 is not None
    assert pending2.path != pending.path
    assert pending2.path.read_bytes() == b"%PDF-1.4 dummy secondary content 2 - updated"

    # Verify prior source file row was deleted
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM source_files WHERE file_category = 'SecondaryPDF'")
    assert cur.fetchone()["cnt"] == 1


def test_audit_read_service_flags(env):
    conn, service, query_svc, checklist_svc, read_svc, audit, tmp_path = env

    digests = read_svc.list_open()
    assert len(digests) == 1
    assert digests[0].is_labeled is False
    assert digests[0].are_photos_uploaded is False
