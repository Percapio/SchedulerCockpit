import os
import pytest
from pathlib import Path

from cockpit.ui.config import resolve_app_data_root, _probe_candidate, AppConfigError, ProbeAttempt
from tests.ui.conftest import install_mkdir_denial, install_open_denial


@pytest.mark.phase19_scenario(1)
def test_scenario_1_env_override(tmp_path, mocked_install_dir, monkeypatch):
    override_path = tmp_path / "override"
    monkeypatch.setenv("COCKPIT_APP_DATA", str(override_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "dummy_appdata"))
    
    outcome = resolve_app_data_root()
    
    assert outcome.chosen_root == override_path
    assert outcome.probe_history[0].candidate_label == "$COCKPIT_APP_DATA override"
    assert outcome.probe_history[0].success


@pytest.mark.phase19_scenario(2)
def test_scenario_2_env_unset_fallback(redirected_appdata, mocked_install_dir):
    # Omitted unset_appdata here because redirected_appdata already unsets COCKPIT_APP_DATA,
    # and unset_appdata deletes APPDATA which contradicts the required assertion.
    outcome = resolve_app_data_root()
    
    assert outcome.chosen_root == redirected_appdata / "Cockpit"
    labels = [p.candidate_label for p in outcome.probe_history]
    assert "$COCKPIT_APP_DATA override" not in labels
    assert outcome.probe_history[0].success


@pytest.mark.phase19_scenario(3)
def test_scenario_3_env_nonwritable_fallback(redirected_appdata, mocked_install_dir, controlled_probe, monkeypatch, tmp_path):
    override_path = tmp_path / "override"
    monkeypatch.setenv("COCKPIT_APP_DATA", str(override_path))
    
    # Layer 2 mock to make it fail
    controlled_probe[override_path] = ProbeAttempt(override_path, "custom", False, "Permission denied", False)
    
    outcome = resolve_app_data_root()
    
    assert outcome.chosen_root == redirected_appdata / "Cockpit"
    assert len(outcome.probe_history) == 2
    assert not outcome.probe_history[0].success


@pytest.mark.phase19_scenario(4)
def test_scenario_4_appdata_writable_short_circuit(redirected_appdata, mocked_install_dir):
    outcome = resolve_app_data_root()
    
    assert outcome.chosen_root == redirected_appdata / "Cockpit"
    assert len(outcome.probe_history) == 1


@pytest.mark.phase19_scenario(5)
def test_scenario_5_appdata_priority(redirected_appdata, mocked_install_dir):
    # Both APPDATA and mocked_install_dir are inherently writable in these tests
    outcome = resolve_app_data_root()
    
    assert outcome.chosen_root == redirected_appdata / "Cockpit"
    assert outcome.chosen_root != mocked_install_dir / "data"


@pytest.mark.phase19_scenario(6)
def test_scenario_6_appdata_touch_fails_fallback(controlled_probe, redirected_appdata, mocked_install_dir):
    appdata_path = redirected_appdata / "Cockpit"
    controlled_probe[appdata_path] = ProbeAttempt(appdata_path, "custom", False, "Permission denied", False)
    
    outcome = resolve_app_data_root()
    
    assert outcome.chosen_root == mocked_install_dir / "data"
    assert len(outcome.probe_history) == 2
    assert not outcome.probe_history[0].success
    assert outcome.probe_history[1].success


@pytest.mark.phase19_scenario(7)
def test_scenario_7_all_fail(controlled_probe, redirected_appdata, mocked_install_dir, monkeypatch, tmp_path):
    override_path = tmp_path / "override"
    monkeypatch.setenv("COCKPIT_APP_DATA", str(override_path))
    
    appdata_path = redirected_appdata / "Cockpit"
    install_data = mocked_install_dir / "data"
    
    controlled_probe[override_path] = ProbeAttempt(override_path, "custom", False, "Deny", False)
    controlled_probe[appdata_path] = ProbeAttempt(appdata_path, "custom", False, "Deny", False)
    controlled_probe[install_data] = ProbeAttempt(install_data, "custom", False, "Deny", False)
    
    with pytest.raises(AppConfigError) as exc:
        resolve_app_data_root()
        
    assert exc.value.error_reason == "all_probes_failed"
    assert len(exc.value.probe_history) == 3


@pytest.mark.phase19_scenario(8)
def test_scenario_8_single_claim(redirected_appdata, mocked_install_dir, controlled_probe):
    appdata_path = redirected_appdata / "Cockpit"
    db_path = appdata_path / "v1" / "local_audit.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()
    
    outcome = resolve_app_data_root()
    
    assert outcome.chosen_root == appdata_path
    assert len(outcome.probe_history) == 1
    assert outcome.probe_history[0].pre_existing_db


@pytest.mark.phase19_scenario(9)
def test_scenario_9_multiple_claims(redirected_appdata, mocked_install_dir):
    appdata_path = redirected_appdata / "Cockpit"
    db_path_1 = appdata_path / "v1" / "local_audit.db"
    db_path_1.parent.mkdir(parents=True, exist_ok=True)
    db_path_1.touch()
    
    install_data = mocked_install_dir / "data"
    db_path_2 = install_data / "v1" / "local_audit.db"
    db_path_2.parent.mkdir(parents=True, exist_ok=True)
    db_path_2.touch()
    
    with pytest.raises(AppConfigError) as exc:
        resolve_app_data_root()
        
    assert exc.value.error_reason == "multiple_claimed"


@pytest.mark.phase19_scenario(10)
def test_scenario_10_claimed_fails(controlled_probe, redirected_appdata, mocked_install_dir):
    appdata_path = redirected_appdata / "Cockpit"
    db_path = appdata_path / "v1" / "local_audit.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()
    
    controlled_probe[appdata_path] = ProbeAttempt(appdata_path, "custom", False, "Permission denied", True)
    
    with pytest.raises(AppConfigError) as exc:
        resolve_app_data_root()
        
    assert exc.value.error_reason == "all_probes_failed"
    assert len(exc.value.probe_history) == 1
    assert not exc.value.probe_history[0].success


@pytest.mark.phase19_scenario(11)
def test_scenario_11_probe_mkdir_fail(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    install_mkdir_denial(monkeypatch, lambda p: str(p) == str(target_dir))
    
    attempt = _probe_candidate(target_dir)
    assert not attempt.success
    assert "Access is denied" in str(attempt.error)


@pytest.mark.phase19_scenario(12)
def test_scenario_12_probe_touch_fail(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    # Will fail when writing .cockpit_probe_
    install_open_denial(monkeypatch, lambda f: ".cockpit_probe_" in f)
    
    attempt = _probe_candidate(target_dir)
    assert not attempt.success
    assert "Access is denied" in str(attempt.error)


@pytest.mark.phase19_scenario(13)
def test_scenario_13_probe_cleanup(tmp_path):
    target_dir = tmp_path / "fresh"
    attempt = _probe_candidate(target_dir)
    
    assert attempt.success
    leftovers = list(target_dir.glob(".cockpit_probe_*"))
    assert not leftovers
