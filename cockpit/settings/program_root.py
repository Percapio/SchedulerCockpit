"""HLC program folder settings and validation (Phase 52 section 6).

The folder where the Juki host keeps one subfolder per job, e.g. B142286\\
holding B142286-T1.E45. Never hard-coded: each Cockpit sets it in Settings.
"""

import os
import pathlib
from enum import Enum, auto

from PyQt6.QtCore import QObject, QSettings, pyqtSignal

from cockpit.ingestion.locator import JOB_NUMBER_GRAMMAR

PROGRAM_ROOT_KEY = "programs/program_root"
# Enough entries to find a job folder on a real share without listing all of it.
_PROBE_ENTRY_LIMIT = 500


class ProgramRootState(Enum):
    CONFIGURED = auto()
    UNSET = auto()
    MALFORMED = auto()


class ProgramRootProbeResult(Enum):
    REACHABLE = auto()
    REACHABLE_BUT_NO_JOB_FOLDERS = auto()
    UNREACHABLE = auto()


class ProgramRootController(QObject):
    changed = pyqtSignal()

    def __init__(self, settings: QSettings):
        super().__init__()
        self._settings = settings

    def program_root(self) -> tuple[ProgramRootState, str | pathlib.Path | None]:
        value = self._settings.value(PROGRAM_ROOT_KEY)
        if not value:
            return (ProgramRootState.UNSET, None)
        path = pathlib.Path(str(value))
        if not path.is_absolute():
            return (ProgramRootState.MALFORMED, str(value))
        return (ProgramRootState.CONFIGURED, path)

    def configured_root(self) -> pathlib.Path | None:
        state, path = self.program_root()
        return path if state == ProgramRootState.CONFIGURED else None

    def set_program_root(self, path: str) -> None:
        self._settings.setValue(PROGRAM_ROOT_KEY, path)
        self.changed.emit()


def probe_program_root(candidate: pathlib.Path) -> ProgramRootProbeResult:
    """Checks that a folder answers and holds job folders.

    post: never raises; OSError means UNREACHABLE
    """
    try:
        if not candidate.is_dir():
            return ProgramRootProbeResult.UNREACHABLE
        with os.scandir(candidate) as entries:
            for index, entry in enumerate(entries):
                if index >= _PROBE_ENTRY_LIMIT:
                    break
                if entry.is_dir() and JOB_NUMBER_GRAMMAR.match(entry.name.upper()):
                    return ProgramRootProbeResult.REACHABLE
    except OSError:
        return ProgramRootProbeResult.UNREACHABLE
    return ProgramRootProbeResult.REACHABLE_BUT_NO_JOB_FOLDERS
