"""Shared ITAR classification normalization.

The Traveler `CLASSIFICATION:` cell contains "YES"/"NO" in current documents,
but historical files and manual edits have used "Y" and "ITAR". All readers
must go through this helper so the interpretation stays consistent
(Phase 32, patch 3.3).
"""

from typing import Any, Mapping

_ITAR_TOKENS = {"YES", "Y", "ITAR"}


def is_itar(metadata: Mapping[str, Any] | None) -> bool:
    """Return True when traveler metadata marks the audit as ITAR."""
    if not metadata:
        return False
    raw = metadata.get("itar_classification")
    if raw is None:
        return False
    return str(raw).strip().upper() in _ITAR_TOKENS


def classification_display(metadata: Mapping[str, Any] | None) -> str:
    """Human-readable classification label for list/digest views."""
    return "ITAR" if is_itar(metadata) else "Non-ITAR"
