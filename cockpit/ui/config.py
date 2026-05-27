"""AppConfig and path resolution."""

import os
import pathlib
import sys
import uuid
import dataclasses
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeAttempt:
    """
    Intent: One row of the probe history. Captured for logging and for the
            all-candidates-failed error dialog.
    Pre:    candidate_path is fully resolved (no env-var expansion remaining).
    Post:   Exactly one of (success=True, error=None) or (success=False, error=<str>).
    """
    candidate_path: pathlib.Path
    candidate_label: str
    success: bool
    error: str | None
    pre_existing_db: bool


@dataclass(frozen=True)
class ResolveOutcome:
    """
    Intent: Carry the chosen root + the full probe history forward into
            AppConfig so downstream logging can report it.
    Pre:    chosen_root is one of the candidate_paths in probe_history.
    Post:   chosen_root exists, is a directory, and has been verified writable
            by the probe routine.
    """
    chosen_root: pathlib.Path
    probe_history: tuple[ProbeAttempt, ...]


class AppConfigError(Exception):
    """
    Configuration resolution failed.
    """
    def __init__(
        self,
        message: str,
        probe_history: tuple[ProbeAttempt, ...] = (),
        error_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.probe_history = probe_history
        self.error_reason = error_reason


@dataclass(frozen=True)
class AppConfig:
    app_data_root: pathlib.Path
    db_path: pathlib.Path
    file_storage_root: pathlib.Path
    coord_map_path: pathlib.Path | None
    log_path: pathlib.Path
    log_level: str
    probe_history: tuple[ProbeAttempt, ...]


@dataclass(frozen=True)
class Candidate:
    """
    Intent: Private record pairing a candidate root path with the human-readable
            label used in logs and the failure dialog.
    """
    path: pathlib.Path
    label: str


def _build_candidates() -> tuple[Candidate, ...]:
    candidates = []
    
    if "COCKPIT_APP_DATA" in os.environ and os.environ["COCKPIT_APP_DATA"].strip():
        candidates.append(Candidate(
            path=pathlib.Path(os.environ["COCKPIT_APP_DATA"]),
            label="$COCKPIT_APP_DATA override"
        ))
        
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            default_path = pathlib.Path(appdata) / "Cockpit"
        else:
            default_path = pathlib.Path.home() / "AppData" / "Roaming" / "Cockpit"
        default_label = "%APPDATA% default"
    elif sys.platform == "darwin":
        default_path = pathlib.Path.home() / "Library" / "Application Support" / "Cockpit"
        default_label = "Library/Application Support"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            default_path = pathlib.Path(xdg_data) / "cockpit"
        else:
            default_path = pathlib.Path.home() / ".local" / "share" / "cockpit"
        default_label = "$XDG_DATA_HOME"
        
    candidates.append(Candidate(path=default_path, label=default_label))
    
    from cockpit.ui.runtime import install_dir
    candidates.append(Candidate(
        path=install_dir() / "data",
        label="<install_dir>/data"
    ))
    
    return tuple(candidates)


def _detect_claimed(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    claimed = []
    for c in candidates:
        if (c.path / "v1" / "local_audit.db").exists():
            claimed.append(c)
    return tuple(claimed)


def _probe_candidate(path: pathlib.Path) -> ProbeAttempt:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return ProbeAttempt(
            candidate_path=path,
            candidate_label="",
            success=False,
            error=repr(e),
            pre_existing_db=False
        )
        
    touch_file = path / f".cockpit_probe_{uuid.uuid4()}"
    try:
        with open(touch_file, "w") as f:
            f.write("probe")
    except Exception as e:
        return ProbeAttempt(
            candidate_path=path,
            candidate_label="",
            success=False,
            error=repr(e),
            pre_existing_db=False
        )
    finally:
        try:
            if touch_file.exists():
                touch_file.unlink()
        except Exception:
            pass
            
    return ProbeAttempt(
        candidate_path=path,
        candidate_label="",
        success=True,
        error=None,
        pre_existing_db=False
    )


def resolve_app_data_root() -> ResolveOutcome:
    """
    Intent: Determine the application data root by probing candidate paths
            in priority order.
    """
    candidates = _build_candidates()
    claimed = _detect_claimed(candidates)

    if len(claimed) > 1:
        history = tuple(
            ProbeAttempt(
                candidate_path=c.path,
                candidate_label=c.label,
                success=False,
                error="multiple_claimed",
                pre_existing_db=True,
            )
            for c in claimed
        )
        raise AppConfigError(
            "Multiple Cockpit data roots detected — set COCKPIT_APP_DATA to choose one",
            probe_history=history,
            error_reason="multiple_claimed",
        )

    targets = claimed or candidates

    history_list: list[ProbeAttempt] = []
    for c in targets:
        attempt = _probe_candidate(c.path)
        attempt = dataclasses.replace(
            attempt,
            candidate_label=c.label,
            pre_existing_db=(c in claimed),
        )
        history_list.append(attempt)
        if attempt.success:
            return ResolveOutcome(chosen_root=c.path, probe_history=tuple(history_list))

    raise AppConfigError(
        "No writable data root could be located",
        probe_history=tuple(history_list),
        error_reason="all_probes_failed",
    )


def get_app_data_root() -> pathlib.Path:
    """Deprecated: use resolve_app_data_root instead. Retained for backwards compatibility."""
    return resolve_app_data_root().chosen_root


def resolve_config(root: pathlib.Path | None = None, probe_history: tuple[ProbeAttempt, ...] = ()) -> AppConfig:
    """Resolve paths and produce an AppConfig. Ensures directories exist."""
    if root is None:
        root = get_app_data_root() / "v1"
    
    if root.exists() and not root.is_dir():
        raise AppConfigError(f"Application data root exists but is not a directory: {root}")
        
    try:
        root.mkdir(parents=True, exist_ok=True)
        file_storage_root = root / "uploads"
        file_storage_root.mkdir(exist_ok=True)
        log_dir = root / "logs"
        log_dir.mkdir(exist_ok=True)
    except Exception as e:
        logger.exception('Exception caught in config')
        raise AppConfigError(f"Cannot write to application data root {root}: {e}")

    log_level = "DEBUG" if os.environ.get("COCKPIT_DEBUG") == "1" else "INFO"
    
    coord_map_path = None
    if "COCKPIT_TRAVELER_MAP_PATH" in os.environ:
        coord_map_path = pathlib.Path(os.environ["COCKPIT_TRAVELER_MAP_PATH"])

    return AppConfig(
        app_data_root=root,
        db_path=root / "local_audit.db",
        file_storage_root=file_storage_root,
        coord_map_path=coord_map_path,
        log_path=log_dir / "cockpit.log",
        log_level=log_level,
        probe_history=probe_history
    )
