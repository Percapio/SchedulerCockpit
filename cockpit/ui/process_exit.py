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
from typing import Callable, NoReturn, Optional, TextIO

from PyQt6.QtCore import QSettings

from cockpit.ui.crash_reporter import write_stderr

logger = logging.getLogger(__name__)

EXIT_LINE_PREFIX = "Exit: code="


@dataclass
class ExitFlushables:
    settings: Optional[QSettings]
    fault_file: Optional[TextIO]


def flush_owned_clipboard() -> None:
    """Leaves the clipboard's contents pasteable after this process ends (Patch 14 §4.3).

    pre:  GUI thread, the thread that set the clipboard
    post: on Windows under Qt's "windows" platform plugin, OleFlushClipboard renders and releases the data
          object this process put on the clipboard (OLE does nothing when the clipboard isn't this
          process's); any other platform or plugin changes nothing; a failing call is logged at INFO
    raises: nothing
    """
    if sys.platform != "win32":
        return
    try:
        from PyQt6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None or QGuiApplication.platformName() != "windows":
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
) -> NoReturn:
    """Ends the process after the Qt event loop has returned, without interpreter finalization (INV-X1).

    pre:  GUI thread; the event loop has returned exit_code
    post: window.shutdown_for_exit() has run when window is not None. Then, in order: the settings are
          synced; the EXIT health line and one exit line (exit_code and every retained thread's label and
          age) are logged; the clipboard is flushed; logging is shut down; faulthandler is disabled and its
          file closed; stdout and stderr are flushed when they exist; hard_exit(exit_code) ends the
          process. A step that raises while logging is up is logged at ERROR with its name; afterwards it
          goes to stderr when there is one, and is otherwise lost. Every later step still runs.
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

    if window is not None:
        step("window teardown", window.shutdown_for_exit)
    if flushables.settings is not None:
        step("settings sync", flushables.settings.sync)
    if health is not None:
        from cockpit.ui.health import HealthReason

        step("exit health line", lambda: health.log_now(HealthReason.EXIT))
    describe = retention.describe_outstanding if retention is not None else (lambda: "none")
    step("exit line", lambda: logger.info("%s%d; retained threads: %s", EXIT_LINE_PREFIX, exit_code, describe()))
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
