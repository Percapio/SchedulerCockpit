import pathlib
import sqlite3
import dataclasses
import hashlib
from typing import Optional

try:
    import fitz
except ImportError:
    fitz = None

from cockpit.persistence.connection import open_connection
from cockpit.persistence.schema import migrate
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.types import (
    ActiveAuditDraft, SourceFileDraft, SourceFileCategory,
    AuditBomComponentDraft, ThtChecklistItemDraft, PdfComponentCoordDraft
)

@dataclasses.dataclass(frozen=True)
class ChurnFixture:
    database_path: pathlib.Path
    file_storage_root: pathlib.Path
    composite_audit_id: int
    secondary_audit_ids: tuple[int, ...]
    reference_page_count: int
    highlight_group_mpn: str
    highlight_group_size: int

class FixtureUnavailable(Exception):
    def __init__(self, stage: str, detail: str):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail

class DummyParserRegistry:
    class DummyParser:
        def parse(self, *args, **kwargs):
            return None
    bom_parser = DummyParser()
    traveler_parser = DummyParser()
    eco_parser = DummyParser()
    pdf_parser = DummyParser()

def _create_synthetic_pdf(path: pathlib.Path, page_count: int) -> None:
    if fitz is None:
        raise FixtureUnavailable("GENERATE_PDF", "PyMuPDF (fitz) is not installed, cannot generate PDFs.")
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page(width=792, height=612)
    doc.save(str(path))
    doc.close()

def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def seed_churn_fixture(destination_root: pathlib.Path, reference_page_count: int) -> ChurnFixture:
    if reference_page_count < 4:
        raise ValueError("reference_page_count must be >= 4")
        
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
        db_path = destination_root / "local_audit.db"
        file_storage = destination_root / "uploads"
        file_storage.mkdir(exist_ok=True)
        
        conn = open_connection(db_path)
        migrate(conn, DummyParserRegistry())
        
        sf_repo = SourceFileRepository(conn)
        bom_repo = AuditBomComponentRepository(conn)
        tht_repo = ThtChecklistRepository(conn)
        pdf_coord_repo = PdfComponentCoordRepository(conn)
        audit_repo = AuditRepository(conn, bom_repo, pdf_coord_repo)
    except Exception as e:
        raise FixtureUnavailable("SEED_DATABASE", str(e))
    
    # 1. Composite Audit
    a1_dir = file_storage / "PN-COMPOSITE" / "unsplit"
    a1_dir.mkdir(parents=True)
    a1_sec_dir = a1_dir / "secondary"
    a1_sec_dir.mkdir(parents=True)
    
    a1 = audit_repo.create(ActiveAuditDraft(part_number="PN-COMPOSITE", work_order_ref="WO123", quantity=1))
    
    try:
        primary_pdf = a1_dir / "primary.pdf"
        _create_synthetic_pdf(primary_pdf, 2)
        pdf_sf = sf_repo.register(SourceFileDraft(
            audit_id=a1.id, file_category=SourceFileCategory.PDF,
            original_filename="primary.pdf", local_storage_path=primary_pdf, file_hash=_sha256(primary_pdf)
        ))
        
        secondary_pdf = a1_sec_dir / "secondary.pdf"
        _create_synthetic_pdf(secondary_pdf, reference_page_count)
        sf_repo.register(SourceFileDraft(
            audit_id=a1.id, file_category=SourceFileCategory.SECONDARY_PDF,
            original_filename="secondary.pdf", local_storage_path=secondary_pdf, file_hash=_sha256(secondary_pdf)
        ))
    except Exception as e:
        raise FixtureUnavailable("GENERATE_PDF", str(e))
    
    try:
        bom_csv = a1_dir / "bom.csv"
        bom_csv.touch()
        bom_sf = sf_repo.register(SourceFileDraft(
            audit_id=a1.id, file_category=SourceFileCategory.BOM,
            original_filename="bom.csv", local_storage_path=bom_csv, file_hash=_sha256(bom_csv)
        ))
        
        bom_drafts = []
        tht_drafts = []
        pdf_drafts = []
        
        # We need >= 80 distinct MPNs. We'll use 10 placements per MPN for 800 placements.
        # highlight_group_mpn will be MPN-0, with >= 64 placements. Let's give it 70.
        # Total placements: MPN-0 (70), MPN-1 to MPN-80 (10 each) = 870 placements.
        
        highlight_group_mpn = "MPN-0"
        highlight_group_size = 70
        
        placements = []
        for i in range(highlight_group_size):
            placements.append((highlight_group_mpn, f"R{i}", 'S'))
            
        for mpn_idx in range(1, 81):
            for j in range(10):
                # Last 50 items will be THT
                mount = 'T' if mpn_idx >= 76 else 'S'
                placements.append((f"MPN-{mpn_idx}", f"C{mpn_idx}_{j}", mount))
                
        for idx, (mpn, rd, mount) in enumerate(placements):
            bom_drafts.append(AuditBomComponentDraft(
                source_file_id=bom_sf.id, component_mpn=mpn,
                ref_des=rd, mount_type=mount, description="Test component", find_number=str(idx+1)
            ))
            if mount == 'T':
                tht_drafts.append(ThtChecklistItemDraft(
                    audit_id=a1.id, source_file_id=bom_sf.id,
                    component_mpn=mpn, description="Test component"
                ))
            
            # Add coords for the highlight group on page 0
            if mpn == highlight_group_mpn:
                pdf_drafts.append(PdfComponentCoordDraft(
                    source_file_id=pdf_sf.id, ref_des=rd, page_index=0,
                    x1=0.0, y1=0.0, x2=10.0, y2=10.0
                ))
                
        bom_repo.bulk_insert(bom_drafts)
        tht_repo.insert_many(tht_drafts)
    except Exception as e:
        raise FixtureUnavailable("INSERT_BOM", str(e))
        
    try:
        if pdf_drafts:
            pdf_coord_repo.bulk_insert(pdf_drafts)
    except Exception as e:
        raise FixtureUnavailable("INSERT_COORDS", str(e))
    
    # Secondary audits
    try:
        a2_dir = file_storage / "PN-SEC1" / "unsplit"
        a2_dir.mkdir(parents=True)
        a2 = audit_repo.create(ActiveAuditDraft(part_number="PN-SEC1", work_order_ref="WO_S1", quantity=1))
        
        a2_primary_pdf = a2_dir / "primary.pdf"
        _create_synthetic_pdf(a2_primary_pdf, 1)
        sf_repo.register(SourceFileDraft(
            audit_id=a2.id, file_category=SourceFileCategory.PDF,
            original_filename="primary.pdf", local_storage_path=a2_primary_pdf, file_hash=_sha256(a2_primary_pdf)
        ))
        
        a3_dir = file_storage / "PN-SEC2" / "unsplit"
        a3_dir.mkdir(parents=True)
        a3 = audit_repo.create(ActiveAuditDraft(part_number="PN-SEC2", work_order_ref="WO_S2", quantity=1))
        
        conn.commit()
        conn.close()
    except Exception as e:
        raise FixtureUnavailable("SEED_DATABASE", str(e))
    
    return ChurnFixture(
        database_path=db_path,
        file_storage_root=file_storage,
        composite_audit_id=a1.id,
        secondary_audit_ids=(a2.id, a3.id),
        reference_page_count=reference_page_count,
        highlight_group_mpn=highlight_group_mpn,
        highlight_group_size=highlight_group_size
    )
