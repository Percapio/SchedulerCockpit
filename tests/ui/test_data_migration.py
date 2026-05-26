import json
import os
import pathlib
import pytest

from cockpit.ui.data_migration import migrate_to_versioned_layout, DataMigrationError

def test_already_migrated(tmp_path):
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir()
    (v1_dir / ".migration_complete").touch()
    
    # Put a legacy file to see if it ignores it
    (tmp_path / "local_audit.db").touch()
    
    outcome = migrate_to_versioned_layout(tmp_path)
    assert outcome.performed is False
    assert outcome.target_root == v1_dir

def test_no_legacy(tmp_path):
    outcome = migrate_to_versioned_layout(tmp_path)
    assert outcome.performed is True
    assert outcome.target_root == tmp_path / "v1"
    assert (tmp_path / "v1" / ".migration_complete").exists()

def test_needs_migration(tmp_path):
    db = tmp_path / "local_audit.db"
    db.write_text("dummy")
    
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "test.log").write_text("log")
    
    outcome = migrate_to_versioned_layout(tmp_path)
    assert outcome.performed is True
    assert len(outcome.moved_items) == 2
    
    v1_dir = tmp_path / "v1"
    assert (v1_dir / "local_audit.db").exists()
    assert (v1_dir / "logs" / "test.log").exists()
    assert not db.exists()
    assert not logs.exists()
    
    sentinel = v1_dir / ".migration_complete"
    data = json.loads(sentinel.read_text(encoding="utf-8"))
    assert "local_audit.db" in data["moved"]
    assert "logs" in data["moved"]

def test_conflict(tmp_path):
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir()
    
    db = tmp_path / "local_audit.db"
    db.write_text("dummy")
    
    with pytest.raises(DataMigrationError, match="Conflict"):
        migrate_to_versioned_layout(tmp_path)
