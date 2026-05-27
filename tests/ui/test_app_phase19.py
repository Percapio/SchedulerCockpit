import pytest
import sys
from PyQt6.QtWidgets import QMessageBox

from cockpit.ui.app import main
from cockpit.ui.config import AppConfigError, ProbeAttempt

def test_main_all_probes_failed(monkeypatch, qtbot):
    def mock_resolve():
        p1 = ProbeAttempt(candidate_path="C:/foo", candidate_label="A", success=False, error="Permission denied", pre_existing_db=False)
        raise AppConfigError(
            "No writable data root could be located",
            probe_history=(p1,),
            error_reason="all_probes_failed"
        )
        
    monkeypatch.setattr("cockpit.ui.app.resolve_app_data_root", mock_resolve)
    
    exit_called = False
    def mock_exit(code):
        nonlocal exit_called
        exit_called = True
        assert code == 2
        raise SystemExit(code)
        
    monkeypatch.setattr(sys, "exit", mock_exit)
    
    def mock_exec(self):
        assert "COCKPIT_APP_DATA" in self.text()
        assert "C:/foo" in self.text()
        assert "C:/foo" in self.detailedText()
        assert "Permission denied" in self.detailedText()
        return QMessageBox.StandardButton.Ok
        
    monkeypatch.setattr(QMessageBox, "exec", mock_exec)
    
    # We mock get_build_info as well just to be safe
    from cockpit._build_info import BuildInfo
    monkeypatch.setattr("cockpit.ui.app.get_build_info", lambda: BuildInfo("test", "test", "2026-05-26T00:00:00Z"))
    
    with pytest.raises(SystemExit):
        main()
    assert exit_called

def test_main_multiple_claimed(monkeypatch, qtbot):
    def mock_resolve():
        p1 = ProbeAttempt(candidate_path="C:/foo", candidate_label="A", success=False, error="multiple_claimed", pre_existing_db=True)
        raise AppConfigError(
            "Multiple roots",
            probe_history=(p1,),
            error_reason="multiple_claimed"
        )
        
    monkeypatch.setattr("cockpit.ui.app.resolve_app_data_root", mock_resolve)
    
    exit_called = False
    def mock_exit(code):
        nonlocal exit_called
        exit_called = True
        assert code == 2
        raise SystemExit(code)
        
    monkeypatch.setattr(sys, "exit", mock_exit)
    
    def mock_exec(self):
        assert "more than one location" in self.text()
        assert "C:/foo\\v1" in self.text()
        return QMessageBox.StandardButton.Ok
        
    monkeypatch.setattr(QMessageBox, "exec", mock_exec)
    
    from cockpit._build_info import BuildInfo
    monkeypatch.setattr("cockpit.ui.app.get_build_info", lambda: BuildInfo("test", "test", "2026-05-26T00:00:00Z"))
    
    with pytest.raises(SystemExit):
        main()
    assert exit_called
