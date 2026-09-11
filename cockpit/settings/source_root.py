"""Source root settings and validation."""

import pathlib
import re
from enum import Enum, auto
from PyQt6.QtCore import QObject, QSettings, pyqtSignal

class SourceRootState(Enum):
    CONFIGURED = auto()
    UNSET = auto()
    MALFORMED = auto()

class RootProbeResult(Enum):
    REACHABLE = auto()
    REACHABLE_BUT_NO_JOB_TREE = auto()
    UNREACHABLE = auto()

class SourceRootController(QObject):
    changed = pyqtSignal()

    def __init__(self, settings: QSettings):
        super().__init__()
        self._settings = settings

    def source_root(self) -> tuple[SourceRootState, str | pathlib.Path | None]:
        val = self._settings.value("ingestion/source_root")
        if not val:
            return (SourceRootState.UNSET, None)
        
        path = pathlib.Path(str(val))
        if not path.is_absolute():
            return (SourceRootState.MALFORMED, str(val))
            
        return (SourceRootState.CONFIGURED, path)
        
    def set_source_root(self, path: str):
        self._settings.setValue("ingestion/source_root", path)
        self.changed.emit()

def probe_source_root(candidate: pathlib.Path) -> RootProbeResult:
    try:
        if not candidate.exists() or not candidate.is_dir():
            return RootProbeResult.UNREACHABLE
            
        entries = list(candidate.iterdir())
    except OSError:
        return RootProbeResult.UNREACHABLE
        
    mask_pattern = re.compile(r"^[A-Z][0-9]{3}xxx$")
    for entry in entries:
        if entry.is_dir() and mask_pattern.match(entry.name):
            return RootProbeResult.REACHABLE
            
    return RootProbeResult.REACHABLE_BUT_NO_JOB_TREE
