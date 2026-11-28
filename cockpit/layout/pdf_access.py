"""At most one thread inside PyMuPDF at a time (Patch 14 §9, INV-P1).

PyMuPDF's documentation: "PyMuPDF does not support running on multiple threads - doing so may cause
incorrect behaviour or even crash Python itself." Cockpit calls it from the render worker, the ingest, the
drawing update and the update check, so every call runs inside a pdf_section.

Confinement: fitz is imported only by layout/parser.py and layout/renderer.py. Every expression that touches
a fitz object is inside a section, and every fitz reference is released inside the section that created or
closed it, so no MuPDF destructor runs outside the lock. Python work on what a step returned runs outside.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterator


class PdfStep(StrEnum):
    OPEN = "open"
    PAGE_TEXT = "page_text"
    PAGE_RAWTEXT = "page_rawtext"
    PAGE_SIZES = "page_sizes"
    PAGE_RASTER = "page_raster"
    CLOSE = "close"
    STORE_EMPTY = "store_empty"


@dataclass(frozen=True)
class PdfReading:
    sections: int
    max_hold_ms: float
    max_wait_ms: float
    longest_step: str = ""


class _TicketLock:
    """A first-come, first-served lock.

    Python's locks aren't fair: measured on the largest local drawing, a render waited 1,076 ms behind a
    parse that released and re-took a plain lock while the longest single step held it 589 ms. Served in
    arrival order, a waiting thread waits for at most the steps already queued ahead of it. Not reentrant:
    pdf_section counts nesting itself and takes the lock only at the outermost entry.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._next_ticket = 0
        self._serving = 0

    def acquire(self) -> None:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            while ticket != self._serving:
                self._condition.wait()

    def release(self) -> None:
        with self._condition:
            self._serving += 1
            self._condition.notify_all()

    def try_acquire(self) -> bool:
        """Takes the lock only when no thread holds it or waits for it; never waits (Patch 15 §3.2)."""
        with self._condition:
            if self._next_ticket != self._serving:
                return False
            self._next_ticket += 1
            return True


_LOCK = _TicketLock()
_DEPTH = threading.local()
_STATS_LOCK = threading.Lock()
_sections = 0
_max_hold_ms = 0.0
_max_wait_ms = 0.0
_longest_step = ""


def held_by_this_thread() -> bool:
    """True inside a pdf_section on the calling thread (for the confinement tests)."""
    return getattr(_DEPTH, "depth", 0) > 0


@contextlib.contextmanager
def pdf_section(step: PdfStep) -> Iterator[None]:
    """Runs one PyMuPDF step while no other thread is inside one (INV-P1).

    pre:  every PyMuPDF object the step touches was created on the calling thread
    post: at most one thread is inside any pdf_section; the same thread may nest; only the outermost entry
          counts and records its wait and hold
    raises: whatever the step raises
    """
    global _sections, _max_hold_ms, _max_wait_ms, _longest_step
    depth = getattr(_DEPTH, "depth", 0)
    if depth:
        _DEPTH.depth = depth + 1
        try:
            yield
        finally:
            _DEPTH.depth = depth
        return
    asked = time.perf_counter()
    _LOCK.acquire()
    entered = time.perf_counter()
    _DEPTH.depth = 1
    try:
        yield
    finally:
        _DEPTH.depth = 0
        held_ms = (time.perf_counter() - entered) * 1000.0
        _LOCK.release()
        wait_ms = (entered - asked) * 1000.0
        with _STATS_LOCK:
            _sections += 1
            if held_ms > _max_hold_ms:
                _max_hold_ms = held_ms
                _longest_step = step.value
            _max_wait_ms = max(_max_wait_ms, wait_ms)


@contextlib.contextmanager
def pdf_section_if_free(step: PdfStep) -> Iterator[bool]:
    """Enters a PDF section only if it is free right now (INV-P1, for the post-ingest probe, Patch 15 §3.2).

    pre:  none
    post: yields true inside the section when no thread held or waited for the lock, or when the calling thread
          is already inside a section; yields false, without waiting and without entering, otherwise. A section
          entered here counts like pdf_section's
    raises: whatever the step raises
    """
    global _sections, _max_hold_ms, _max_wait_ms, _longest_step
    depth = getattr(_DEPTH, "depth", 0)
    if depth:
        _DEPTH.depth = depth + 1
        try:
            yield True
        finally:
            _DEPTH.depth = depth
        return
    if not _LOCK.try_acquire():
        yield False
        return
    entered = time.perf_counter()
    _DEPTH.depth = 1
    try:
        yield True
    finally:
        _DEPTH.depth = 0
        held_ms = (time.perf_counter() - entered) * 1000.0
        _LOCK.release()
        with _STATS_LOCK:
            _sections += 1
            if held_ms > _max_hold_ms:
                _max_hold_ms = held_ms
                _longest_step = step.value


def pdf_reading() -> PdfReading:
    """Sections, longest hold and longest wait since the previous call; the figures then reset."""
    global _sections, _max_hold_ms, _max_wait_ms, _longest_step
    with _STATS_LOCK:
        reading = PdfReading(_sections, _max_hold_ms, _max_wait_ms, _longest_step)
        _sections, _max_hold_ms, _max_wait_ms, _longest_step = 0, 0.0, 0.0, ""
    return reading
