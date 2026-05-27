import builtins
import pathlib
from pathlib import Path
from typing import Callable, Dict

import pytest

from cockpit.ui.config import ProbeAttempt
import cockpit.ui.config
import cockpit.ui.runtime


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "phase19_scenario(N): mark test to map to Phase 19 section 8 scenario N"
    )


@pytest.fixture
def redirected_appdata(tmp_path: Path, monkeypatch) -> Path:
    """
    Intent: Point %APPDATA% at a controlled subdirectory of tmp_path so the
            platform-default candidate is isolated from the developer's real
            %APPDATA%\\Cockpit data. Used by tests where %APPDATA% should
            resolve successfully.
    """
    appdata_dir = tmp_path / "appdata"
    appdata_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata_dir))
    monkeypatch.delenv("COCKPIT_APP_DATA", raising=False)
    return appdata_dir


@pytest.fixture
def unset_appdata(monkeypatch) -> None:
    """
    Intent: Simulate the rare case where %APPDATA% env var is absent entirely.
            Used by tests of the env-var-missing fall-through.
    """
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("COCKPIT_APP_DATA", raising=False)


@pytest.fixture
def controlled_probe(monkeypatch) -> Dict[Path, ProbeAttempt]:
    """
    Intent: Replace cockpit.ui.config._probe_candidate with a programmable
            mock backed by a {candidate_path: ProbeAttempt} mapping that the
            test populates inline.
    """
    control_dict: Dict[Path, ProbeAttempt] = {}

    def mock_probe(path: Path) -> ProbeAttempt:
        if path in control_dict:
            return control_dict[path]
        return ProbeAttempt(
            candidate_path=path,
            candidate_label="",
            success=True,
            error=None,
            pre_existing_db=False
        )

    monkeypatch.setattr(cockpit.ui.config, "_probe_candidate", mock_probe)
    return control_dict


@pytest.fixture
def mocked_install_dir(tmp_path: Path, monkeypatch) -> Path:
    """
    Intent: Substitute cockpit.ui.runtime.install_dir() with a function that
            returns a controlled subdirectory of tmp_path.
    """
    install_dir_path = tmp_path / "install"
    install_dir_path.mkdir(parents=True, exist_ok=True)

    def mock_install_dir() -> Path:
        return install_dir_path

    monkeypatch.setattr(cockpit.ui.runtime, "install_dir", mock_install_dir)
    return install_dir_path


def install_mkdir_denial(monkeypatch, target_predicate: Callable[[Path], bool]) -> None:
    """
    Intent: Patch pathlib.Path.mkdir to raise PermissionError for any path
            satisfying target_predicate; pass through to the real mkdir for
            all other paths.
    """
    original_mkdir = pathlib.Path.mkdir

    def mock_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if target_predicate(self):
            raise PermissionError(13, 'Access is denied')
        return original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(pathlib.Path, "mkdir", mock_mkdir)


def install_open_denial(monkeypatch, target_predicate: Callable[[str], bool]) -> None:
    """
    Intent: Patch builtins.open to raise PermissionError when called against
            a file path satisfying target_predicate. All other open() calls
            pass through to the real implementation.
    """
    original_open = builtins.open

    def mock_open(file, mode='r', buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        if target_predicate(str(file)):
            raise PermissionError(13, 'Access is denied')
        return original_open(file, mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline, closefd=closefd, opener=opener)

    monkeypatch.setattr(builtins, "open", mock_open)
