import pytest
import pathlib
import sqlite3
import logging
import re
from cockpit.ui.config import AppConfig, ProbeAttempt
from cockpit.ui.bootstrap import bootstrap

def test_bootstrap_probe_history_logging(tmp_path, monkeypatch):
    
    # Create the db to satisfy sqlite3 connection in bootstrap
    root = tmp_path / "v1"
    root.mkdir(parents=True)
    db_path = root / "local_audit.db"
    conn = sqlite3.connect(db_path)
    conn.close()
    
    p1 = ProbeAttempt(candidate_path=pathlib.Path("C:/failed1"), candidate_label="A", success=False, error="Permission denied", pre_existing_db=False)
    p2 = ProbeAttempt(candidate_path=root.parent, candidate_label="B", success=True, error=None, pre_existing_db=False)
    
    config = AppConfig(
        app_data_root=root,
        db_path=db_path,
        file_storage_root=root / "uploads",
        coord_map_path=None,
        log_path=root / "logs" / "cockpit.log",
        log_level="INFO",
        probe_history=(p1, p2)
    )
    
    (root / "uploads").mkdir()
    (root / "logs").mkdir()
    
    messages = []
    class MockLogger:
        def info(self, msg, *args):
            messages.append(msg % args if args else msg)
        def warning(self, msg, *args):
            pass
        def exception(self, msg, *args):
            pass

    original_get_logger = logging.getLogger

    def mock_get_logger(name=None):
        if name == "cockpit":
            return MockLogger()
        return original_get_logger(name)
        
    monkeypatch.setattr("cockpit.ui.bootstrap.logging.getLogger", mock_get_logger)
    
    app = bootstrap(config)
    
    assert app.config == config
    
    found = False
    for msg in messages:
        if "Application data root:" in msg:
            assert "A -> Permission denied" in msg
            assert "B -> OK" in msg
            assert str(root.parent) in msg
            found = True
            
    assert found
