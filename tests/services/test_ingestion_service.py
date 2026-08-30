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
        
    file_storage_root = tmp_path / "cockpit_data"
    
    # We need a coordinate map but add_pdf_to_audit doesn't use it
    service = IngestionService(
        conn=conn,
        audit_repo=audit_repo,
        source_file_repo=source_file_repo,
        tht_repo=tht_repo,
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
    
    # Check coords belong to the new file
    coords = service.pdf_coord_repo.list_for_source_file(sf2.id)
    assert len(coords) > 0  # Assuming DummyParser gives coords
    
    # Ensure no duplicate source files for PDF
    all_sfs = service.source_file_repo.list_for_audit(audit_id)
    pdf_sfs = [sf for sf in all_sfs if sf.file_category == SourceFileCategory.PDF]
    assert len(pdf_sfs) == 1

def test_add_pdf_to_audit_parse_error_rollback(ingestion_service):
    service, audit_id, tmp_path = ingestion_service
    
    # Create fake pdf
    pdf_path = tmp_path / "error.pdf"
    pdf_path.write_bytes(b"fake pdf content")
    
    with pytest.raises(ValueError, match="Parse failed"):
        service.add_pdf_to_audit(audit_id, pdf_path)
        
    pdf_sf = service.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
    assert pdf_sf is None

def test_ingest_all_smt_board_persists_empty_tht_checklist(ingestion_service, monkeypatch):
    service, _, tmp_path = ingestion_service
    from cockpit.ingestion.progress import ProgressStage
    from cockpit.ingestion.parsers.results import BomItem, IngestionIntent
    from cockpit.persistence.types import ActiveAuditDraft
    
    # 1. Create files
    bom_path = tmp_path / "TEST-123 AUDIT BOM.xlsx"
    bom_path.write_text("a")
    trav_path = tmp_path / "TEST-123 Traveler.xlsx"
    trav_path.write_text("a")
    eco_path = tmp_path / "TEST-123 ECO.docx"
    eco_path.write_text("a")
    
    paths = [bom_path, trav_path, eco_path]
    
    # 2. Monkeypatch
    monkeypatch.setattr("cockpit.ingestion.parsers.audit_bom.parse", lambda p: None)
    monkeypatch.setattr("cockpit.ingestion.parsers.eco_build_notes.parse", lambda p: type("EcoResult", (), {"declared_part_number": "", "row_count": 1, "raw_table_count": 1})())
    monkeypatch.setattr("cockpit.ingestion.parsers.traveler.parse", lambda p, cm: None)
    
    # Mock intent
    bom_items = [
        BomItem(
            component_mpn="SMT-1",
            description="SMT Component",
            mount_type="S",
            ref_des_raw="C1",
            ref_des_list=("C1",),
            find_number=1
        )
    ]
    
    intent_mock = IngestionIntent(
        audit_draft=ActiveAuditDraft(part_number="TEST-123", work_order_ref="WO", quantity=1),
        bom_items=bom_items,
        )
    
    monkeypatch.setattr("cockpit.ingestion.cross_validation.reconcile", lambda b, e, t, cm: intent_mock)
    
    events = []
    def progress(evt):
        events.append(evt)
        
    audit = service.ingest(paths, progress)
    
    assert audit is not None
    assert service.tht_repo.list_for_audit(audit.id) == []    
    persisted_events = [e for e in events if e.stage == ProgressStage.PERSISTED]
    assert len(persisted_events) == 1
    assert persisted_events[0].detail["tht_item_count"] == 0


def test_ingest_mixed_board_reports_tht_count_not_bom_count(ingestion_service, monkeypatch):
    service, _, tmp_path = ingestion_service
    from cockpit.ingestion.progress import ProgressStage
    from cockpit.ingestion.parsers.results import BomItem, IngestionIntent
    from cockpit.persistence.types import ActiveAuditDraft
    
    bom_path = tmp_path / "TEST-123 AUDIT BOM.xlsx"
    bom_path.write_text("a")
    trav_path = tmp_path / "TEST-123 Traveler.xlsx"
    trav_path.write_text("a")
    eco_path = tmp_path / "TEST-123 ECO.docx"
    eco_path.write_text("a")
    
    paths = [bom_path, trav_path, eco_path]
    
    monkeypatch.setattr("cockpit.ingestion.parsers.audit_bom.parse", lambda p: None)
    monkeypatch.setattr("cockpit.ingestion.parsers.eco_build_notes.parse", lambda p: type("EcoResult", (), {"declared_part_number": "", "row_count": 1, "raw_table_count": 1})())
    monkeypatch.setattr("cockpit.ingestion.parsers.traveler.parse", lambda p, cm: None)
    
    bom_items = [
        BomItem(component_mpn="SMT-1", description="SMT Component", mount_type="S", ref_des_raw="C1", ref_des_list=("C1",), find_number=1),
        BomItem(component_mpn="SMT-2", description="SMT Component 2", mount_type="S", ref_des_raw="C2", ref_des_list=("C2",), find_number=2),
        BomItem(component_mpn="SMT-3", description="SMT Component 3", mount_type="S", ref_des_raw="C3", ref_des_list=("C3",), find_number=3),
        BomItem(component_mpn="THT-1", description="THT Component", mount_type="T", ref_des_raw="R1", ref_des_list=("R1",), find_number=4),
        BomItem(component_mpn="THT-2", description="THT Component 2", mount_type="T", ref_des_raw="R2", ref_des_list=("R2",), find_number=5),
    ]
    
    intent_mock = IngestionIntent(
        audit_draft=ActiveAuditDraft(part_number="TEST-123", work_order_ref="WO", quantity=1),
        bom_items=bom_items,
        )
    
    monkeypatch.setattr("cockpit.ingestion.cross_validation.reconcile", lambda b, e, t, cm: intent_mock)
    
    events = []
    def progress(evt):
        events.append(evt)
        
    audit = service.ingest(paths, progress)
    
    assert audit is not None
    tht_items = service.tht_repo.list_for_audit(audit.id)
    assert len(tht_items) == 2
    
    persisted_events = [e for e in events if e.stage == ProgressStage.PERSISTED]
    assert len(persisted_events) == 1
    assert persisted_events[0].detail["tht_item_count"] == 2
    assert persisted_events[0].detail["tht_item_count"] != len(bom_items)

def test_tht_insert_many_still_rejects_empty(ingestion_service):
    service, _, _ = ingestion_service
    from cockpit.persistence.errors import InvalidArgumentError
    
    with pytest.raises(InvalidArgumentError, match="Cannot be empty"):
        service.tht_repo.insert_many([])
