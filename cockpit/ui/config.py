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
    sqlite_cache_kib: int = -16384
    sqlite_mmap_bytes: int = 134_217_728
    wal_autocheckpoint_pages: int = 1000
    idle_maintenance_interval_ms: int = 60000
    # Every record is formatted and written twice when this is on, and in a
    # frozen install the second copy lands inside the installation directory.
    # Defaults off; the primary log at log_path is unaffected either way.
    log_duplicate_to_install_dir: bool = False
    # Patch 14 §6: the collector runs only on the GUI thread, driven by this tick. The budgets are alert
    # thresholds (a collection can't be interrupted): they decide what is logged and counted.
    gc_tick_ms: int = 100
    gc_young_threshold: int = 2_000
    gc_middle_threshold: int = 10
    gc_full_interval_ms: int = 1_800_000
    gc_young_budget_ms: int = 50
    gc_full_budget_ms: int = 150
    # Patch 14 §10.2: one health line this often; COCKPIT_HEALTH_INTERVAL_MS overrides it.
    health_interval_ms: int = 900_000
    # Patch 15 §8 (F26): logs and crash reports are kept this many days; COCKPIT_LOG_RETENTION_DAYS overrides it.
    log_retention_days: int = 91

    @property
    def crash_dir(self) -> pathlib.Path:
        """Where the crash reporter writes once the configuration is resolved (Patch 15 §8)."""
        return self.file_storage_root.parent / "crash_reports"


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
            label=OVERRIDE_LABEL
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


OVERRIDE_LABEL = "$COCKPIT_APP_DATA override"


def resolve_app_data_root() -> ResolveOutcome:
    """
    Intent: Determine the application data root by probing candidate paths
            in priority order.
    Post:   Patch 15 §10.2 (F29): with COCKPIT_APP_DATA set and not blank, that folder is the root when its
            probe succeeds, whatever the other candidates hold; the probe history holds that one attempt.
            Without it, discovery is unchanged.
    Raises: AppConfigError("override_unusable") when the override's probe fails; otherwise as before.
    """
    candidates = _build_candidates()
    override = next((c for c in candidates if c.label == OVERRIDE_LABEL), None)
    if override is not None:
        attempt = _probe_candidate(override.path)
        attempt = dataclasses.replace(
            attempt,
            candidate_label=override.label,
            pre_existing_db=(override.path / "v1" / "local_audit.db").exists(),
        )
        if attempt.success:
            return ResolveOutcome(chosen_root=override.path, probe_history=(attempt,))
        raise AppConfigError(
            f"COCKPIT_APP_DATA points at {override.path}, which Cockpit can't write to",
            probe_history=(attempt,),
            error_reason="override_unusable",
        )

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
        probe_history=probe_history,
        health_interval_ms=health_interval_from_environment(AppConfig.health_interval_ms),
        log_retention_days=retention_days_from_environment(AppConfig.log_retention_days),
    )


def retention_days_from_environment(default_days: int) -> int:
    """COCKPIT_LOG_RETENTION_DAYS, when it names a positive whole number of days (Patch 15 §8)."""
    raw = os.environ.get("COCKPIT_LOG_RETENTION_DAYS", "").strip()
    if not raw:
        return default_days
    try:
        value = int(raw)
    except ValueError:
        logger.warning("COCKPIT_LOG_RETENTION_DAYS=%r isn't a whole number; keeping %d days", raw, default_days)
        return default_days
    if value <= 0:
        logger.warning("COCKPIT_LOG_RETENTION_DAYS=%d isn't positive; keeping %d days", value, default_days)
        return default_days
    return value


def health_interval_from_environment(default_ms: int) -> int:
    """COCKPIT_HEALTH_INTERVAL_MS, when it names a positive whole number of milliseconds (Patch 14 §10.2)."""
    raw = os.environ.get("COCKPIT_HEALTH_INTERVAL_MS", "").strip()
    if not raw:
        return default_ms
    try:
        value = int(raw)
    except ValueError:
        logger.warning("COCKPIT_HEALTH_INTERVAL_MS=%r isn't a whole number; using %d ms", raw, default_ms)
        return default_ms
    if value <= 0:
        logger.warning("COCKPIT_HEALTH_INTERVAL_MS=%d isn't positive; using %d ms", value, default_ms)
        return default_ms
    return value
