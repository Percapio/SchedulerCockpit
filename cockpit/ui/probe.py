"""The post-ingest probe (Patch 15 §3.2; F21, F32).

Production keeps about 12.5 MB per ingest window (F21), and M2a, driving the same path on the same data, kept
about 1 MB, so the holder can only be named from production. After each ingest, at the first quiet moment on the
Audit List, the probe logs memory at four points: before, after a full collection, after MuPDF's store is
emptied, and after the process heaps are compacted. It adds the wrapper classes whose live count rose. The census
reads the line (scripts/audit/log_census.py), and the next patch fixes what a week of them names.

Emptying the store also bounds F32: ingest parsing left 17 MB in it in M2a.
"""

from __future__ import annotations

import collections
import ctypes
import gc
import logging
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Optional

logger = logging.getLogger("cockpit.probe")

PROBE_PREFIX = "probe:"
PROBE_TICK_MS = 60_000
PROBE_QUIET_MS = 60_000
PROBE_INTERVAL_MS = 14_400_000
WRAPPER_RISE = 10
CENSUS_BUDGET_MS = 250
CENSUS_CHECK_EVERY = 10_000
NA = "na"


class ProbeReason(StrEnum):
    INGEST = "ingest"
    INTERVAL = "interval"


class ProbeStep(StrEnum):
    COLLECTION = "collection"
    STORE = "store"
    HEAPS = "heaps"
    CENSUS = "census"


class CensusMode(StrEnum):
    BASELINE = "baseline"
    ON = "on"
    PARTIAL = "partial"
    OFF = "off"


@dataclass(frozen=True)
class Readings:
    priv_mb: Optional[float]
    ws_mb: Optional[float]
    handles: Optional[int]


@dataclass
class CensusState:
    """The last completed census, kept for the next probe, and whether the census still runs this session."""

    counts: Optional[dict[str, int]] = None
    enabled: bool = True


@dataclass(frozen=True)
class ProbeReading:
    reason: ProbeReason
    operations: object
    before: Readings
    after_collection: Readings
    after_store_emptied: Readings
    after_heaps_compacted: Readings
    census: CensusMode
    rises: tuple[tuple[str, int, int], ...] = ()
    failed: tuple[ProbeStep, ...] = ()
    duration_ms: int = 0


def take_readings(counters: Callable[[], object]) -> Readings:
    try:
        c = counters()
        return Readings(c.private_mb, c.working_set_mb, c.handles)
    except Exception:
        return Readings(None, None, None)


def compact_heaps() -> bool:
    """Compacts every heap of this process (GetProcessHeaps, then HeapCompact on each).

    post: true when every call succeeded; false when GetProcessHeaps returns 0, any HeapCompact returns 0 with a
          non-zero GetLastError, or the platform isn't Windows
    raises: nothing
    """
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessHeaps.restype = ctypes.c_uint32
        kernel32.GetProcessHeaps.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        kernel32.HeapCompact.restype = ctypes.c_size_t
        kernel32.HeapCompact.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        count = kernel32.GetProcessHeaps(0, None)
        if count == 0:
            return False
        heaps = (ctypes.c_void_p * count)()
        count = kernel32.GetProcessHeaps(count, heaps)
        if count == 0:
            return False
        ok = True
        for index in range(count):
            ctypes.set_last_error(0)
            if kernel32.HeapCompact(heaps[index], 0) == 0 and ctypes.get_last_error() != 0:
                ok = False
        return ok
    except Exception:
        return False


def take_census(budget_ms: int, perf: Callable[[], float] = time.perf_counter) -> tuple[dict[str, int], bool]:
    """Counts live sip wrappers by class, stopping once budget_ms has passed.

    post: (counts, complete); complete is false when the budget ran out, and counts then cover only the objects
          walked so far, so every count is a lower bound
    raises: nothing
    """
    from PyQt6 import sip

    start = perf()
    counts: collections.Counter[str] = collections.Counter()
    wrapper = sip.simplewrapper
    try:
        objects = gc.get_objects()
    except Exception:
        return {}, False
    for index, obj in enumerate(objects):
        if index and index % CENSUS_CHECK_EVERY == 0 and (perf() - start) * 1000.0 > budget_ms:
            return dict(counts), False
        if isinstance(obj, wrapper):
            counts[type(obj).__name__] += 1
    return dict(counts), True


def probe_after_work(
    reason: ProbeReason,
    operations: object,
    collect_full: Callable[[], None],
    empty_store: Callable[[], bool],
    counters: Callable[[], object],
    census_state: CensusState,
    compact: Callable[[], bool] = compact_heaps,
    census: Callable[[int], tuple[dict[str, int], bool]] = take_census,
    perf: Callable[[], float] = time.perf_counter,
) -> ProbeReading:
    """Measures what the work since the last probe left behind, and releases what can be released (§3.2).

    pre:  GUI thread (the full collection must run there, INV-T1); the caller checked the preconditions
    post: readings before, after one full collection, after empty_store, and after compact; a store that was busy
          or failed is failed=store, a heap call that failed is failed=heaps, a failed collection failed=collection.
          The census, while enabled: the first complete one is a baseline (no rises); later ones report each class
          up by WRAPPER_RISE or more since the last complete census; one that runs past CENSUS_BUDGET_MS reports
          partial and turns the census off for the session. A complete census replaces census_state.counts
    raises: nothing
    """
    started = perf()
    failed: list[ProbeStep] = []
    before = take_readings(counters)
    try:
        collect_full()
    except Exception:
        failed.append(ProbeStep.COLLECTION)
    after_collection = take_readings(counters)
    try:
        stored = empty_store()
    except Exception:
        stored = False
    if not stored:
        failed.append(ProbeStep.STORE)
    after_store = take_readings(counters)
    try:
        compacted = compact()
    except Exception:
        compacted = False
    if not compacted:
        failed.append(ProbeStep.HEAPS)
    after_heaps = take_readings(counters)

    mode = CensusMode.OFF
    rises: list[tuple[str, int, int]] = []
    if census_state.enabled:
        try:
            counts, complete = census(CENSUS_BUDGET_MS)
        except Exception:
            counts, complete = {}, False
            failed.append(ProbeStep.CENSUS)
        previous = census_state.counts
        if previous is not None:
            for name, live in sorted(counts.items()):
                rise = live - previous.get(name, 0)
                if rise >= WRAPPER_RISE:
                    rises.append((name, live, rise))
        if complete:
            mode = CensusMode.BASELINE if previous is None else CensusMode.ON
            census_state.counts = counts
        else:
            mode = CensusMode.PARTIAL
            census_state.enabled = False
    return ProbeReading(
        reason=reason,
        operations=operations,
        before=before,
        after_collection=after_collection,
        after_store_emptied=after_store,
        after_heaps_compacted=after_heaps,
        census=mode,
        rises=tuple(rises),
        failed=tuple(failed),
        duration_ms=int((perf() - started) * 1000.0),
    )


def _value(value: object) -> str:
    return NA if value is None else str(value)


def format_probe(reading: ProbeReading) -> str:
    """The probe as one line, keys in §3.2's order."""
    ops = reading.operations
    points = (reading.before, reading.after_collection, reading.after_store_emptied, reading.after_heaps_compacted)
    rises = ",".join(f"{name}:{live}(+{rise})" for name, live, rise in reading.rises) or "none"
    failed = ",".join(step.value for step in reading.failed) or "none"
    return " ".join((
        PROBE_PREFIX,
        f"reason={reading.reason.value}",
        f"n_ingest={_value(getattr(ops, 'ingests', None))}",
        f"n_open={_value(getattr(ops, 'opens', None))}",
        f"n_update_run={_value(getattr(ops, 'update_runs', None))}",
        "priv_mb=" + "/".join(_value(p.priv_mb) for p in points),
        "ws_mb=" + "/".join(_value(p.ws_mb) for p in points),
        "handles=" + "/".join(_value(p.handles) for p in points),
        f"census={reading.census.value}",
        f"rises={rises}",
        f"failed={failed}",
        f"ms={reading.duration_ms}",
    ))


def parse_probe(message: str) -> dict[str, object]:
    """A probe line's fields: the four-point readings as lists (None for "na"), the rest as text or numbers."""
    fields: dict[str, object] = {}
    body = message[len(PROBE_PREFIX):] if message.startswith(PROBE_PREFIX) else message
    for pair in body.split():
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key in ("priv_mb", "ws_mb", "handles"):
            fields[key] = [None if part == NA else float(part) for part in value.split("/")]
        elif key in ("n_ingest", "n_open", "n_update_run", "ms"):
            fields[key] = None if value == NA else int(value)
        elif key == "failed":
            fields[key] = [] if value == "none" else value.split(",")
        elif key == "rises":
            fields[key] = [] if value == "none" else value.split(",")
        else:
            fields[key] = value
    return fields
