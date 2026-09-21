import logging
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


def is_class_3(traveler_metadata: Any) -> bool:
    """True iff the traveler declares IPC Class 3."""
    if not isinstance(traveler_metadata, Mapping):
        return False
    raw_class = traveler_metadata.get("assembly_class")
    try:
        return int(raw_class) == 3
    except (TypeError, ValueError):
        return False


class WashState(Enum):
    """What the traveler's PROCESS row says about washing.

    UNKNOWN is a real answer, not a default: a traveler ingested before
    process_clean was mapped carries no value at all, and reporting that as
    NO_CLEAN states something the document never said.

    UNRECOGNISED is the traveler that said something the Document Control
    dropdown cannot produce. It renders exactly as UNKNOWN does and differs
    only in that a write path logs it, because it means a released traveler is
    off-template. It is never reported as CLEAN: Patch 10 section 3.1 — a
    predicate that must pick a side cannot be distinguished from one that read
    the document.
    """

    CLEAN = "CLEAN"
    NO_CLEAN = "NO_CLEAN"
    UNKNOWN = "UNKNOWN"
    UNRECOGNISED = "UNRECOGNISED"


_NEGATIONS = frozenset({"no", "non", "none", "not", "noclean", "nonclean"})
_AFFIRMATIONS = frozenset({"clean"})


def wash_state(traveler_metadata: Any) -> WashState:
    """Reads the wash designation from the traveler's own words.

    pre:  traveler_metadata is the parsed traveler mapping, or anything else
    post: UNKNOWN when process_clean is absent, not a string, or blank;
          NO_CLEAN when the text carries a negation token; CLEAN when it
          carries the clean token and no negation; UNRECOGNISED otherwise

    The wash cell is a closed dropdown offering "Clean" and "No Clean". Both
    resolve here at any casing and with any separator; anything else is an
    off-template traveler and says so rather than guessing.
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
    if tokens & _AFFIRMATIONS:
        return WashState.CLEAN
    return WashState.UNRECOGNISED


def is_clean_process(traveler_metadata: Any) -> bool:
    """True iff the build uses a clean/wash process.

    pre:  traveler_metadata is the parsed traveler mapping, or anything else
    post: true iff wash_state(traveler_metadata) is CLEAN. NO_CLEAN, UNKNOWN
          and UNRECOGNISED all yield false, so neither a no-clean build, an
          unparsed traveler, nor an off-template cell inflates a runtime.

    One reading of process_clean, two consumers. The earlier implementation
    asked only whether the cell was non-empty, which made "No Clean" a wash
    process and applied clean_process_multiplier_tht to builds that are never
    washed.
    """
    return wash_state(traveler_metadata) is WashState.CLEAN


def unrecognised_wash_text(traveler_metadata: Any) -> str | None:
    """The verbatim wash cell when it is UNRECOGNISED, else None.

    post: a non-empty string iff wash_state(traveler_metadata) is UNRECOGNISED
    """
    if wash_state(traveler_metadata) is not WashState.UNRECOGNISED:
        return None
    return str(traveler_metadata.get("process_clean")).strip()


def log_unrecognised_wash_designation(audit_identity: str, traveler_metadata: Any) -> None:
    """Records that a released traveler carried an off-dropdown wash cell.

    pre:  called from a repository write path, once per audit written
    post: one WARNING carrying the audit identity and the verbatim cell text
          when the designation is UNRECOGNISED; nothing otherwise. Never
          raises, so a write is not lost to a logging fault.

    Deliberately at the write boundary, not inside wash_state: the predicate
    runs on every Audit List repaint, and suppressing that would need a
    module-level memo of already-logged spellings shared between the UI thread
    and the ingestion worker.
    """
    text = unrecognised_wash_text(traveler_metadata)
    if text is None:
        return
    logger.warning(
        "Traveler for %s carries an unreadable wash designation %r; "
        "expected one of the Document Control dropdown values. "
        "Treated as no wash process and no clean multiplier applied.",
        audit_identity,
        text,
    )
