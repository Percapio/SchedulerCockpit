"""One busy state for every long operation (Phase 55b §3).

`_operation_in_flight` did two jobs: it kept operations from starting over each other, and it was
Phase 54's write gate. Only an ingest needs the gate, since only an ingest holds a savepoint on its
worker thread over the shared connection. This module keeps the two apart, and words why an entry
can't start.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Optional


class Operation(StrEnum):
    INGEST = "ingest"
    JOB_FETCH = "job_fetch"
    DRAWING_FETCH = "drawing_fetch"
    DRAWING_UPDATE = "drawing_update"
    UPDATE_CHECK = "update_check"
    PLACEMENTS_CHECK = "placements_check"


class Entry(StrEnum):
    NEW_AUDIT = "new_audit"
    CHECK_ALL = "check_all"
    CHECK_FAMILY = "check_family"
    DRAWING_FETCH = "drawing_fetch"
    DRAWING_MANUAL = "drawing_manual"
    RESET = "reset"


OPERATION_WORDING: dict[Operation, str] = {
    Operation.INGEST: "An ingest is running.",
    Operation.JOB_FETCH: "A job is being fetched from the share.",
    Operation.DRAWING_FETCH: "A drawing is being fetched from the share.",
    Operation.DRAWING_UPDATE: "A drawing is being read.",
    Operation.UPDATE_CHECK: "A document update check is running.",
    Operation.PLACEMENTS_CHECK: "Placements are being read for the new audit.",
}

PARKED_WORDING = "Waiting for the share to answer the last check."
SOURCE_ROOT_UNSET = "Source root is not configured. Settings › Source Root."
SOURCE_ROOT_INVALID = "Source root is not a valid path. Settings › Source Root."


@dataclass(frozen=True)
class BusyState:
    operation: Optional[Operation] = None
    parked_check: bool = False

    def begin(self, operation: Operation) -> "BusyState":
        return replace(self, operation=operation)

    def end(self) -> "BusyState":
        return replace(self, operation=None)

    def parked(self, parked: bool) -> "BusyState":
        return replace(self, parked_check=parked)


def reconciler_can_write(busy: BusyState) -> bool:
    """Phase 54's write gate: false iff an ingest's savepoint may be open on its worker thread."""
    return busy.operation != Operation.INGEST


def unavailable_reason(
    entry: Entry,
    busy: BusyState,
    source_root_state: Optional[str] = None,
    part_number: Optional[str] = None,
    fetch_reason: Optional[str] = None,
) -> Optional[str]:
    """Why an entry can't start now, or None when it can (Phase 55b §3.2).

    source_root_state is the SourceRootState's name, lowercased ("configured", "unset", "malformed"),
    needed by the checks; fetch_reason is Fetch's own reason, needed by DRAWING_FETCH.
    """
    if busy.operation is not None:
        return OPERATION_WORDING[busy.operation]
    if entry in (Entry.CHECK_ALL, Entry.CHECK_FAMILY):
        if busy.parked_check:
            return PARKED_WORDING
        if source_root_state != "configured":
            return SOURCE_ROOT_UNSET if source_root_state in (None, "unset") else SOURCE_ROOT_INVALID
        if entry == Entry.CHECK_FAMILY:
            from cockpit.ingestion.locator import JOB_NUMBER_GRAMMAR
            if not JOB_NUMBER_GRAMMAR.match(str(part_number or "")):
                return f"{part_number} is not a job number."
    if entry == Entry.DRAWING_FETCH and fetch_reason:
        return fetch_reason
    return None
