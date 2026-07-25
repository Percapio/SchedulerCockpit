from collections.abc import Mapping
from typing import Any


def is_class_3(traveler_metadata: Any) -> bool:
    """True iff the traveler declares IPC Class 3."""
    if not isinstance(traveler_metadata, Mapping):
        return False
    raw_class = traveler_metadata.get("assembly_class")
    try:
        return int(raw_class) == 3
    except (TypeError, ValueError):
        return False


def is_clean_process(traveler_metadata: Any) -> bool:
    """True iff the build uses a clean/wash process."""
    if not isinstance(traveler_metadata, Mapping):
        return False
    raw_clean = traveler_metadata.get("process_clean")
    if not isinstance(raw_clean, str):
        return False
    return raw_clean.strip() != ""
