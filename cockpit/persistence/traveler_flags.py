import re
from collections.abc import Mapping
from enum import Enum
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


class WashState(Enum):
    """What the traveler's PROCESS row says about washing.

    UNKNOWN is a real answer, not a default: a traveler ingested before
    process_clean was mapped carries no value at all, and reporting that as
    NO_CLEAN states something the document never said.
    """

    CLEAN = "CLEAN"
    NO_CLEAN = "NO_CLEAN"
    UNKNOWN = "UNKNOWN"


_NEGATIONS = frozenset({"no", "non", "none", "not", "noclean", "nonclean"})


def wash_state(traveler_metadata: Any) -> WashState:
    """Reads the wash designation from the traveler's own words.

    pre:  traveler_metadata is the parsed traveler mapping, or anything else
    post: UNKNOWN when process_clean is absent, not a string, or blank;
          NO_CLEAN when its text negates cleaning; CLEAN otherwise

    Deliberately not derived from is_clean_process. That predicate asks only
    whether the cell is non-empty, so a traveler spelling out "NO CLEAN" would
    read as a wash process.
    """
    if not isinstance(traveler_metadata, Mapping):
        return WashState.UNKNOWN
    raw_clean = traveler_metadata.get("process_clean")
    if not isinstance(raw_clean, str):
        return WashState.UNKNOWN

    text = raw_clean.strip()
    if not text:
        return WashState.UNKNOWN

    tokens = {t for t in re.split(r"[^a-zA-Z]+", text.lower()) if t}
    if tokens & _NEGATIONS:
        return WashState.NO_CLEAN
    return WashState.CLEAN
