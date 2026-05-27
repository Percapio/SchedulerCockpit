import sys
import pathlib
import pytest

from cockpit.ui.runtime import install_dir, RuntimeKind

def test_install_dir_frozen(monkeypatch):
    monkeypatch.setattr("cockpit.ui.runtime.runtime_kind", lambda: RuntimeKind.FROZEN_ONEDIR)
    monkeypatch.setattr(sys, "executable", "C:/fake/Cockpit.exe")
    assert install_dir() == pathlib.Path("C:/fake")

def test_install_dir_source(monkeypatch):
    monkeypatch.setattr("cockpit.ui.runtime.runtime_kind", lambda: RuntimeKind.SOURCE)
    # The actual __file__ is cockpit/ui/runtime.py
    import cockpit.ui.runtime
    expected = pathlib.Path(cockpit.ui.runtime.__file__).resolve().parents[2]
    assert install_dir() == expected
