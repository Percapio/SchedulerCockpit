"""Ending the process without interpreter finalization (Patch 14 §4, INV-X1).

After a clean close nothing in Python's or Qt's teardown is allowed to run: no atexit handler, no
finalizer, no Qt destructor. Exit then can't depend on destruction order, or on which threads are still
running (a QThread destroyed while it runs aborts the process; that was the Phase 55 exit crash and the
2026-09-12 fatal). Everything that must survive the exit is flushed here, explicitly, first.

The rule this adds: work that must happen at exit goes into finish_process's list, never into an atexit
handler or a destructor. Patch 14 §4.3-4.4 is the inventory.
"""

from __future__ import annotations

import ctypes
import faulthandler
import logging
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, NoReturn, Optional, TextIO

from PyQt6.QtCore import QSettings

from cockpit.ui.crash_reporter import write_stderr

logger = logging.getLogger(__name__)

EXIT_LINE_PREFIX = "Exit: code="


@dataclass
class ExitFlushables:
    settings: Optional[QSettings]
    fault_file: Optional[TextIO]


class ExitReason(StrEnum):
    QUIT = "quit"
    SESSION_END = "session_end"


@dataclass
class ExitRecord:
    """Whether the process's exit is already in the log (Patch 15 §4): shared by the session-end handler and
    finish_process, so a session end that returns from exec() isn't logged twice."""

    logged: bool = False
    reason: Optional[ExitReason] = None


def clipboard_owned_by_this_process() -> bool:
    """Whether the clipboard's owner window belongs to this process (Patch 15 §7).

    pre:  Windows
    post: true when GetClipboardOwner names a window of this process; false when there's no owner, another
          process owns it, or the query fails
    raises: nothing
    """
    try:
        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")
        user32.GetClipboardOwner.restype = ctypes.c_void_p
        user32.GetClipboardOwner.argtypes = []
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        owner = user32.GetClipboardOwner()
        if not owner:
            return False
        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(owner, ctypes.byref(pid))
        return pid.value == kernel32.GetCurrentProcessId()
    except Exception:
        return False


def flush_owned_clipboard() -> None:
    """Leaves the clipboard's contents pasteable after this process ends (Patch 14 §4.3; Patch 15 §7).

    pre:  GUI thread, the thread that set the clipboard
    post: on Windows under Qt's "windows" platform plugin, when GetClipboardOwner's window belongs to this
          process, OleFlushClipboard renders and releases the data object, and a failing HRESULT is logged at
          INFO. When the clipboard has no owner or another process's, nothing is called and nothing is logged
          (OleFlushClipboard returns E_FAIL then, which every exit used to log, F25). Any other platform or
          plugin changes nothing.
    raises: nothing
    """
    if sys.platform != "win32":
        return
    try:
        from PyQt6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None or QGuiApplication.platformName() != "windows":
            return
        if not clipboard_owned_by_this_process():
            return
        ole32 = ctypes.WinDLL("ole32")
        ole32.OleFlushClipboard.restype = ctypes.c_long
        ole32.OleFlushClipboard.argtypes = []
        result = ole32.OleFlushClipboard()
        if result != 0:
            logger.info("The clipboard wasn't flushed at exit (HRESULT 0x%08X)", result & 0xFFFFFFFF)
    except Exception as error:
        logger.info("The clipboard wasn't flushed at exit: %s", error)


def _flush_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()


def finish_process(
    exit_code: int,
    window,
    flushables: ExitFlushables,
    health,
    retention,
    hard_exit: Callable[[int], NoReturn] = os._exit,
    *,
    shutdown_logging: Callable[[], None] = logging.shutdown,
    disable_faulthandler: Callable[[], None] = faulthandler.disable,
    exit_record: Optional[ExitRecord] = None,
) -> NoReturn:
    """Ends the process after the Qt event loop has returned, without interpreter finalization (INV-X1).

    pre:  GUI thread; the event loop has returned exit_code
    post: window.shutdown_for_exit() has run when window is not None. Then, in order: the settings are
          synced; the EXIT health line and one exit line ("Exit: code=<n>; reason=quit; retained threads: …")
          are logged and the clipboard is flushed, unless exit_record says a session end already did so
          (Patch 15 §4.3); logging is shut down; faulthandler is disabled and its file closed; stdout and
          stderr are flushed when they exist; hard_exit(exit_code) ends the process. A step that raises while
          logging is up is logged at ERROR with its name; afterwards it goes to stderr when there is one, and
          is otherwise lost. Every later step still runs.
    raises: nothing; never returns

    shutdown_logging and disable_faulthandler exist for in-process tests, which must not tear down
    pytest's own logging and faulthandler.
    """
    logging_up = True

    def step(name: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as error:
            if logging_up:
                logger.exception("Exit step %r failed", name)
            else:
                write_stderr(f"Exit step {name!r} failed: {error!r}\n")

    # Patch 15 §4.3: after a Windows session end (end_session_now), exec() can still return before Windows ends the
    # process. The window teardown is skipped then: the connection is already closed, and the teardown can wait
    # seconds on threads while Windows is ending the process. The settings are already synced.
    session_ended = exit_record is not None and exit_record.reason == ExitReason.SESSION_END
    if window is not None and not session_ended:
        step("window teardown", window.shutdown_for_exit)
    if flushables.settings is not None and not session_ended:
        step("settings sync", flushables.settings.sync)
    # Patch 15 §4.3: a session end already logged its exit and flushed the clipboard.
    already_logged = exit_record is not None and exit_record.logged
    if health is not None and not already_logged:
        from cockpit.ui.health import HealthReason

        step("exit health line", lambda: health.log_now(HealthReason.EXIT))
    describe = retention.describe_outstanding if retention is not None else (lambda: "none")
    if not already_logged:
        step("exit line", lambda: logger.info(
            "%s%d; reason=%s; retained threads: %s", EXIT_LINE_PREFIX, exit_code, ExitReason.QUIT.value, describe()
        ))
        if exit_record is not None:
            exit_record.logged, exit_record.reason = True, ExitReason.QUIT
        step("clipboard", flush_owned_clipboard)
    step("logging shutdown", shutdown_logging)
    logging_up = False

    def close_fault_file() -> None:
        disable_faulthandler()
        if flushables.fault_file is not None:
            flushables.fault_file.close()

    step("faulthandler", close_fault_file)
    step("streams", _flush_streams)
    hard_exit(exit_code)
    raise SystemExit(exit_code)
