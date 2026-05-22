"""AppConfig and path resolution."""

import os
import pathlib
import sys
from dataclasses import dataclass


class AppConfigError(Exception):
    """Configuration resolution failed."""
    pass


@dataclass(frozen=True)
class AppConfig:
    db_path: pathlib.Path
    file_storage_root: pathlib.Path
    coord_map_path: pathlib.Path | None
    log_path: pathlib.Path
    log_level: str


def _get_app_data_root() -> pathlib.Path:
    """Resolve the per-OS application-data directory."""
    if "COCKPIT_APP_DATA" in os.environ:
        return pathlib.Path(os.environ["COCKPIT_APP_DATA"])

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return pathlib.Path(appdata) / "Cockpit"
        return pathlib.Path.home() / "AppData" / "Roaming" / "Cockpit"
        
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "Cockpit"
        
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return pathlib.Path(xdg_data) / "cockpit"
    return pathlib.Path.home() / ".local" / "share" / "cockpit"


def resolve_config() -> AppConfig:
    """Resolve paths and produce an AppConfig. Ensures directories exist."""
    
    root = _get_app_data_root()
    
    if root.exists() and not root.is_dir():
        raise AppConfigError(f"Application data root exists but is not a directory: {root}")
        
    try:
        root.mkdir(parents=True, exist_ok=True)
        file_storage_root = root / "uploads"
        file_storage_root.mkdir(exist_ok=True)
        log_dir = root / "logs"
        log_dir.mkdir(exist_ok=True)
    except Exception as e:
        raise AppConfigError(f"Cannot write to application data root {root}: {e}")

    log_level = "DEBUG" if os.environ.get("COCKPIT_DEBUG") == "1" else "INFO"
    
    coord_map_path = None
    if "COCKPIT_TRAVELER_MAP_PATH" in os.environ:
        coord_map_path = pathlib.Path(os.environ["COCKPIT_TRAVELER_MAP_PATH"])

    return AppConfig(
        db_path=root / "local_audit.db",
        file_storage_root=file_storage_root,
        coord_map_path=coord_map_path,
        log_path=log_dir / "cockpit.log",
        log_level=log_level
    )
