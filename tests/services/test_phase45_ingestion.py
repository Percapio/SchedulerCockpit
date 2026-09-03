import sqlite3
import pytest
from typing import Any

from cockpit.persistence.connection import hydrating_row_factory
from cockpit.persistence.schema import migrate
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.types import AuditBomComponentDraft
from cockpit.protocols import ParserRegistry
from cockpit.persistence.errors import DuplicateRefDesError

def _setup_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory
    class DummyParser:
        def parse(self, path): return None
    registry = ParserRegistry(bom_parser=DummyParser(), eco_parser=None, traveler_parser=None, pdf_layout_parser=None, coord_map=None)
    migrate(conn, registry)
    
    # We need a source_file_id
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO active_audits (id, part_number, work_order_ref, quantity, created_at, updated_at) VALUES (1, 'PN', 'WO', 1, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')")
    conn.execute("INSERT INTO source_files (id, audit_id, original_filename, local_storage_path, file_category, file_hash, ingested_at) VALUES (1, 1, 'f', 'f', 'BOM', 'h', '2025-01-01T00:00:00Z')")
    conn.execute("PRAGMA foreign_keys = ON")
    
    return conn, 1

def test_split_row_ingestion(tmp_path):
    conn, sf_id = _setup_db(tmp_path)
    bom_repo = AuditBomComponentRepository(conn)
    
    drafts = [
        AuditBomComponentDraft(source_file_id=sf_id, component_mpn="PART-A", ref_des="R14", mount_type="S", description="", find_number="37A"),
        AuditBomComponentDraft(source_file_id=sf_id, component_mpn="PART-B", ref_des="R14", mount_type="S", description="", find_number="37B"),
    ]
    
    bom_repo.bulk_insert(drafts)
    
    lines = bom_repo.list_for_source_file(sf_id)
    assert len(lines) == 2
    assert {(l.find_number, l.component_mpn, l.ref_des) for l in lines} == {("37A", "PART-A", "R14"), ("37B", "PART-B", "R14")}

def test_duplicate_ref_des_on_same_find_number(tmp_path):
    conn, sf_id = _setup_db(tmp_path)
    bom_repo = AuditBomComponentRepository(conn)
    
    drafts = [
        AuditBomComponentDraft(source_file_id=sf_id, component_mpn="PART-A", ref_des="R14", mount_type="S", description="", find_number="37"),
        AuditBomComponentDraft(source_file_id=sf_id, component_mpn="PART-A", ref_des="R14", mount_type="S", description="", find_number="37"),
    ]
    
    with pytest.raises(DuplicateRefDesError) as excinfo:
        bom_repo.bulk_insert(drafts)
        
    assert "R14" in str(excinfo.value)
