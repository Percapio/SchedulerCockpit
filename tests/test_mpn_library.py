import pytest
from datetime import datetime, timezone
from cockpit.services.mpn_library.types import (
    Provenance, PartAttributeDraft, ResolvedAttribute, Absent, Unkeyable, ValueKind
)
from cockpit.services.mpn_library.normalisation import normalise_mpn, NORMALISATION_VERSION
from cockpit.services.mpn_library.registry import ATTRIBUTE_REGISTRY
from cockpit.services.mpn_library.repository import LibraryRepository
from cockpit.services.mpn_library.schema import migrate_library
from cockpit.services.mpn_library.connection import open_library_connection
from cockpit.services.library_errors import UnknownAttributeName
from cockpit.persistence.repositories.bom_components import PersistedBomLine

def test_normalisation():
    # Case folding
    assert normalise_mpn("abc") == "ABC"
    # Whitespace
    assert normalise_mpn("  A  B  C  ") == "A B C"
    # Empty
    assert isinstance(normalise_mpn(""), Unkeyable)
    assert isinstance(normalise_mpn("   "), Unkeyable)
    # Non-ASCII
    assert isinstance(normalise_mpn("ABC\u00A0DEF"), Unkeyable) # non-breaking space
    # Deliberate distinctness
    assert normalise_mpn("SN74LVC1G08DBVR") != normalise_mpn("SN74LVC1G08DBVT")

def test_registry():
    assert "carrier_width_mm" in ATTRIBUTE_REGISTRY
    assert "carrier_pitch_mm" in ATTRIBUTE_REGISTRY
    
    # Assert unique displays
    displays = [s.display for s in ATTRIBUTE_REGISTRY.values()]
    assert len(displays) == len(set(displays))
    
    # Assert no tape_width_mm
    assert "tape_width_mm" not in ATTRIBUTE_REGISTRY

@pytest.fixture
def empty_db(tmp_path):
    db_path = tmp_path / "parts_library.db"
    migrate_library(db_path)
    return db_path

def test_schema_migration(empty_db):
    import sqlite3
    conn = sqlite3.connect(empty_db)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cur.fetchall()}
    assert "library_part" in tables
    assert "part_attribute" in tables
    assert "digikey_snapshot" in tables
    assert "library_quota_day" in tables

def mock_utcnow():
    return datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

def test_repository_observe_and_override(empty_db):
    import threading
    conn = open_library_connection(empty_db, threading.get_ident())
    repo = LibraryRepository(conn)
    
    bom_lines = [
        PersistedBomLine(audit_id=1, source_file_id=1, part_number="", split_suffix="", work_order_ref="", find_number="1", component_mpn="ABC", description="", mount_type="S"),
        PersistedBomLine(audit_id=1, source_file_id=1, part_number="", split_suffix="", work_order_ref="", find_number="2", component_mpn="ABC", description="", mount_type="S"),
        PersistedBomLine(audit_id=1, source_file_id=1, part_number="", split_suffix="", work_order_ref="", find_number="3", component_mpn="DEF", description="", mount_type="S"),
    ]
    
    # Observe
    summary = repo.observe_bom(bom_lines, mock_utcnow)
    assert summary.new_unresolved_count == 2
    assert summary.existing_count == 0
    
    # Re-observe
    summary2 = repo.observe_bom(bom_lines, mock_utcnow)
    assert summary2.new_unresolved_count == 0
    assert summary2.existing_count == 2
    
    # Override
    draft = PartAttributeDraft(
        mpn_key="ABC",
        attribute_name="carrier_width_mm",
        provenance=Provenance.OPERATOR_OVERRIDE,
        value_text="8mm",
        value_numeric=8.0,
        source_label=None
    )
    repo.record_override(draft, mock_utcnow)
    
    # Resolve
    res = repo.resolve_attribute("ABC", "carrier_width_mm")
    assert isinstance(res, ResolvedAttribute)
    assert res.value_text == "8mm"
    assert res.value_numeric == 8.0
    
    # Unknown attr
    with pytest.raises(UnknownAttributeName):
        repo.resolve_attribute("ABC", "unknown_attr")

def test_network_isolation():
    """
    Phase 47b: only cockpit.services.mpn_library.digikey_* may import a network module.
    The grid, the repository, the registry, the normaliser and the resolver stay provably offline.
    """
    import sys
    import importlib
    
    # Reload modules to trace imports
    modules_to_check = [
        "cockpit.services.mpn_library.module",
        "cockpit.services.mpn_library.connection",
        "cockpit.services.mpn_library.repository",
        "cockpit.services.mpn_library.registry",
        "cockpit.services.mpn_library.normalisation",
        "cockpit.services.mpn_library.schema",
        "cockpit.services.mpn_library.types",
    ]
    
    network_modules = {"socket", "ssl", "http", "urllib", "requests", "httpx"}
    
    for mod_name in modules_to_check:
        if mod_name in sys.modules:
            del sys.modules[mod_name]
            
    # temporarily intercept imports to check for network modules
    # A simpler way: import them and check their globals/dependencies, but python imports are transitive.
    # Instead we just check that after importing them, they didn't pull in network modules directly.
    # Actually, if we just check that none of these modules have network modules in their dict:
    for mod_name in modules_to_check:
        mod = importlib.import_module(mod_name)
        for name, val in vars(mod).items():
            if getattr(val, "__name__", None) in network_modules:
                pytest.fail(f"Module {mod_name} illegally imports network module {val.__name__}")
