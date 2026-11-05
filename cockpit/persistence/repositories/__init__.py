"""Repositories package."""
from .audits import AuditRepository
from .source_files import SourceFileRepository
from .tht_checklist import ThtChecklistRepository

__all__ = [
    "AuditRepository",
    "AuditRequirementsRepository",
    "SourceFileRepository",
    "ThtChecklistRepository",
    "set_xray_images_done",
]


def __getattr__(name: str):
    if name in ("AuditRequirementsRepository", "set_xray_images_done"):
        from . import requirements
        return getattr(requirements, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
