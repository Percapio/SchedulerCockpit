"""Runtime utilities for locating bundled resources."""

import enum
import pathlib
import sys


class ResourceNotFoundError(Exception):
    """Raised when a bundled resource is not found."""
    pass


class RuntimeKind(enum.Enum):
    SOURCE = "source"
    FROZEN_ONEDIR = "frozen_onedir"


def runtime_kind() -> RuntimeKind:
    """Determine if running from source or from a PyInstaller frozen bundle."""
    if getattr(sys, "frozen", False):
        return RuntimeKind.FROZEN_ONEDIR
    return RuntimeKind.SOURCE


def bundle_root() -> pathlib.Path:
    """Return the root directory containing packaged files."""
    if runtime_kind() == RuntimeKind.FROZEN_ONEDIR:
        exe_dir = pathlib.Path(sys.executable).parent
        internal = exe_dir / "_internal"
        if not internal.is_dir():
            raise RuntimeError(f"Expected PyInstaller _internal directory not found at {internal}")
        return internal / "cockpit"
    else:
        return pathlib.Path(__file__).resolve().parent.parent


def bundled_resource(relative_path: str) -> pathlib.Path:
    """Locate a packaged resource by its path relative to the cockpit package root.
    
    Args:
        relative_path: e.g., 'ui/theme.json'
        
    Raises:
        ResourceNotFoundError: If the file does not exist.
    """
    # ensure relative_path doesn't start with / or \ to prevent path joining issues
    rel = relative_path.lstrip("/\\")
    path = bundle_root() / rel
    if not path.exists():
        raise ResourceNotFoundError(f"Bundled resource not found: {path}")
    return path
