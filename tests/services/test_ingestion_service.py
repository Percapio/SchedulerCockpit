import sqlite3
import pytest
import pathlib
from dataclasses import dataclass

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

class DummyLayoutParser:
    @dataclass
    class Coord:
        ref_des: str
        page_index: int
        x1: float
        y1: float
        x2: float
        y2: float
        
    @dataclass
    class Result:
        found_ref_des: set[str]
        coordinates: list
        
    def parse(self, pdf_path: pathlib.Path, expected_ref_des: set[str]):
        if "error" in pdf_path.name:
            raise ValueError("Parse failed")
        return self.Result(
            found_ref_des={"C1"},
            coordinates=[self.Coord("C1", 0, 0.0, 0.0, 10.0, 10.0)]
        )

@pytest.fixture
def ingestion_service(tmp_path):
    db_path = tmp_path / "test.db"
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
    
    # We need a coordinate map but add_pdf_to_audit doesn't use it
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
        file_storage_root=file_storage_root
    )
    
    # create an audit
    audit = audit_repo.create(ActiveAuditDraft(
        part_number="TEST-123",
        work_order_ref="WO-001",
        quantity=10
    ))
    
    return service, audit.id, tmp_path

def test_add_pdf_to_audit_success(ingestion_service):
    service, audit_id, tmp_path = ingestion_service
    
    # Create fake pdf
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"fake pdf content")
    
    service.add_pdf_to_audit(audit_id, pdf_path)
    
    # Verify PDF was added
    pdf_sf = service.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
    assert pdf_sf is not None
    assert pdf_sf.original_filename == "test.pdf"
    
    # Verify coords were saved
    coords = service.pdf_coord_repo.list_for_source_file(pdf_sf.id)
    assert len(coords) == 1
    assert coords[0].ref_des == "C1"

def test_add_pdf_to_audit_replace(ingestion_service):
    service, audit_id, tmp_path = ingestion_service
    
    # Add first PDF
    pdf1 = tmp_path / "test1.pdf"
    pdf1.write_bytes(b"content 1")
    service.add_pdf_to_audit(audit_id, pdf1)
    
    sf1 = service.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
    assert sf1.original_filename == "test1.pdf"
    
    # Add second PDF (replacement)
    pdf2 = tmp_path / "test2.pdf"
    pdf2.write_bytes(b"content 2")
    service.add_pdf_to_audit(audit_id, pdf2)
    
    sf2 = service.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
    assert sf2.original_filename == "test2.pdf"
    assert sf1.id != sf2.id
    
    # Check old coords are gone (cascaded by DB)
    coords1 = service.pdf_coord_repo.list_for_source_file(sf1.id)
    assert len(coords1) == 0

def test_add_pdf_to_audit_parse_error_rollback(ingestion_service):
    service, audit_id, tmp_path = ingestion_service
    
    # Create fake pdf
    pdf_path = tmp_path / "error.pdf"
    pdf_path.write_bytes(b"fake pdf content")
    
    with pytest.raises(ValueError, match="Parse failed"):
        service.add_pdf_to_audit(audit_id, pdf_path)
        
    pdf_sf = service.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
    assert pdf_sf is None
