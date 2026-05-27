import os
import pathlib
import builtins
import cockpit.ui.config
import cockpit.ui.runtime

from cockpit.ui.config import ProbeAttempt
from tests.ui.conftest import install_mkdir_denial, install_open_denial

# Capture top-level originals
original_probe = cockpit.ui.config._probe_candidate
original_install_dir = cockpit.ui.runtime.install_dir
original_mkdir = pathlib.Path.mkdir
original_open = builtins.open

# 1. redirected_appdata
original_appdata_1 = os.environ.get("APPDATA")

def test_redirected_appdata_consumes(redirected_appdata, tmp_path):
    assert os.environ.get("APPDATA") == str(tmp_path / "appdata")

def test_redirected_appdata_restores():
    assert os.environ.get("APPDATA") == original_appdata_1

# 2. unset_appdata
original_appdata_2 = os.environ.get("APPDATA")
original_cockpit_app_data_2 = os.environ.get("COCKPIT_APP_DATA")

def test_unset_appdata_consumes(unset_appdata):
    assert "APPDATA" not in os.environ
    assert "COCKPIT_APP_DATA" not in os.environ

def test_unset_appdata_restores():
    assert os.environ.get("APPDATA") == original_appdata_2
    assert os.environ.get("COCKPIT_APP_DATA") == original_cockpit_app_data_2

# 3. controlled_probe
def test_controlled_probe_consumes(controlled_probe, tmp_path):
    assert cockpit.ui.config._probe_candidate is not original_probe
    p = tmp_path / "custom"
    controlled_probe[p] = ProbeAttempt(p, "custom_label", False, "Custom error", False)
    
    result_custom = cockpit.ui.config._probe_candidate(p)
    assert not result_custom.success
    assert result_custom.error == "Custom error"
    
    p2 = tmp_path / "default"
    result_default = cockpit.ui.config._probe_candidate(p2)
    assert result_default.success
    assert result_default.candidate_path == p2

def test_controlled_probe_restores(tmp_path):
    assert cockpit.ui.config._probe_candidate is original_probe
    p = tmp_path / "fresh"
    attempt = cockpit.ui.config._probe_candidate(p)
    assert attempt.success
    assert not list(p.glob(".cockpit_probe_*"))

# 4. mocked_install_dir
def test_mocked_install_dir_consumes(mocked_install_dir, tmp_path):
    assert cockpit.ui.runtime.install_dir is not original_install_dir
    assert cockpit.ui.runtime.install_dir() == tmp_path / "install"

def test_mocked_install_dir_restores():
    assert cockpit.ui.runtime.install_dir is original_install_dir

# 5. install_mkdir_denial
def test_install_mkdir_denial_consumes(monkeypatch, tmp_path):
    target = tmp_path / "target"
    install_mkdir_denial(monkeypatch, lambda p: str(p).startswith(str(target)))
    
    assert pathlib.Path.mkdir is not original_mkdir
    
    import pytest
    with pytest.raises(PermissionError):
        target.mkdir()
        
    (tmp_path / "ok").mkdir() # Should succeed

def test_install_mkdir_denial_restores(tmp_path):
    assert pathlib.Path.mkdir is original_mkdir
    (tmp_path / "target").mkdir() # Should succeed now

# 6. install_open_denial
def test_install_open_denial_consumes(monkeypatch, tmp_path):
    install_open_denial(monkeypatch, lambda f: "denied.txt" in str(f))
    
    assert builtins.open is not original_open
    
    import pytest
    with pytest.raises(PermissionError):
        with open(tmp_path / "denied.txt", "w") as f:
            f.write("no")
            
    with open(tmp_path / "ok.txt", "w") as f:
        f.write("ok")

def test_install_open_denial_restores(tmp_path):
    assert builtins.open is original_open
    with open(tmp_path / "denied.txt", "w") as f:
        f.write("yes")
