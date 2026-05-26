import pytest
import sqlite3

from cockpit.persistence.connection import hydrating_row_factory
from cockpit.persistence.schema import migrate
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.repositories.notes_checklist import BuildNotesChecklistRepository
from cockpit.protocols import ParserRegistry
from cockpit.services.checklist import ChecklistService
from cockpit.services.audit_metadata import AuditMetadataService

def setup_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory
    class DummyParser:
        def parse(self, path): return None
        
    registry = ParserRegistry(DummyParser(), None, None, None, None)
    migrate(conn, registry)
    
    bom_repo = AuditBomComponentRepository(conn)
    pdf_repo = PdfComponentCoordRepository(conn)
    audit_repo = AuditRepository(conn, bom_repo, pdf_repo)
    source_file_repo = SourceFileRepository(conn)
    tht_repo = ThtChecklistRepository(conn)
    notes_repo = BuildNotesChecklistRepository(conn)
    
    checklist_svc = ChecklistService(conn, audit_repo, tht_repo, notes_repo, source_file_repo)
    audit_metadata_svc = AuditMetadataService(conn, audit_repo)
    
    conn.execute("INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) VALUES (1, 'p', 'w', '', 1, 'InProgress', '2020-01-01T00:00:00', '2020-01-01T00:00:00')")
    
    return checklist_svc, audit_metadata_svc

@pytest.mark.parametrize("notes", [None, "", "X", "Multiline\nstring", "unicode 🚀"])
def test_load_active_audit_general_notes(tmp_path, notes):
    checklist_svc, metadata_svc = setup_db(tmp_path)
    metadata_svc.set_general_notes(1, notes)
    view = checklist_svc.load_active_audit(1)
    assert view.general_notes == notes
