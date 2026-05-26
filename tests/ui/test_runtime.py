import pathlib
import pytest
import sys

from cockpit.ui.runtime import runtime_kind, bundle_root, bundled_resource, RuntimeKind, ResourceNotFoundError

def test_runtime_kind_source(monkeypatch):
    if hasattr(sys, "frozen"):
        monkeypatch.delattr(sys, "frozen", raising=False)
    assert runtime_kind() == RuntimeKind.SOURCE

def test_runtime_kind_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert runtime_kind() == RuntimeKind.FROZEN_ONEDIR

def test_bundle_root_source(monkeypatch):
    if hasattr(sys, "frozen"):
        monkeypatch.delattr(sys, "frozen", raising=False)
    # the root should be cockpit package directory
    root = bundle_root()
    assert root.name == "cockpit"
    assert (root / "ui" / "runtime.py").exists()

def test_bundle_root_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    exe_dir = tmp_path / "dist" / "Cockpit"
    internal = exe_dir / "_internal"
    internal.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Cockpit.exe"))
    
    root = bundle_root()
    assert root == internal / "cockpit"

def test_bundle_root_frozen_missing_internal(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    exe_dir = tmp_path / "dist" / "Cockpit"
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Cockpit.exe"))
    
    with pytest.raises(RuntimeError, match="Expected PyInstaller _internal"):
        bundle_root()

def test_bundled_resource_found(monkeypatch):
    if hasattr(sys, "frozen"):
        monkeypatch.delattr(sys, "frozen", raising=False)
    
    # Should find runtime.py itself
    path = bundled_resource("ui/runtime.py")
    assert path.exists()

def test_bundled_resource_not_found(monkeypatch):
    if hasattr(sys, "frozen"):
        monkeypatch.delattr(sys, "frozen", raising=False)
        
    with pytest.raises(ResourceNotFoundError, match="Bundled resource not found"):
        bundled_resource("does/not/exist.json")
