import pytest
import sqlite3
import math
from cockpit.persistence.connection import hydrating_row_factory
from cockpit.persistence.schema import migrate
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.protocols import ParserRegistry
from cockpit.persistence.errors import InvalidArgumentError, AuditNotFound


@pytest.fixture
def audit_repo(tmp_path):
    db_path = tmp_path / "test_ops.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = hydrating_row_factory
    class DummyParser:
        def parse(self, path): return None
    registry = ParserRegistry(DummyParser(), None, None, None, None)
    migrate(conn, registry)
    
    bom_repo = AuditBomComponentRepository(conn)
    pdf_repo = PdfComponentCoordRepository(conn)
    repo = AuditRepository(conn, bom_repo, pdf_repo)
    
    # insert an audit
    conn.execute(
        "INSERT INTO active_audits (id, part_number, work_order_ref, split_suffix, quantity, status, created_at, updated_at) "
        "VALUES (1, 'p', 'w', '', 100, 'Not Clear', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    return repo


def test_set_ops_per_board_min_valid_and_clear(audit_repo):
    audit = audit_repo.find_by_id(1)
    assert audit.ops_per_board_min is None

    audit_repo.set_ops_per_board_min(1, 1.5)
    audit = audit_repo.find_by_id(1)
    assert audit.ops_per_board_min == 1.5

    audit_repo.set_ops_per_board_min(1, 0.0)
    audit = audit_repo.find_by_id(1)
    assert audit.ops_per_board_min == 0.0

    audit_repo.set_ops_per_board_min(1, None)
    audit = audit_repo.find_by_id(1)
    assert audit.ops_per_board_min is None


def test_set_ops_per_board_min_validation(audit_repo):
    with pytest.raises(InvalidArgumentError):
        audit_repo.set_ops_per_board_min(1, -0.1)

    with pytest.raises(InvalidArgumentError):
        audit_repo.set_ops_per_board_min(1, float('inf'))

    with pytest.raises(InvalidArgumentError):
        audit_repo.set_ops_per_board_min(1, float('nan'))


def test_set_ops_per_board_min_not_found(audit_repo):
    with pytest.raises(AuditNotFound):
        audit_repo.set_ops_per_board_min(999, 1.0)
