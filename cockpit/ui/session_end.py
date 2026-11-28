"""The exit when Windows ends the session (Patch 15 §4, F23).

On WM_QUERYENDSESSION Qt emits commitDataRequest; on WM_ENDSESSION(TRUE) it emits aboutToQuit, with the window
still open, and then calls quit(). Windows ends the process once WM_ENDSESSION returns, so the code after
app.exec() (finish_process) may never run: no exit line, and a WAL left behind, which is what a crash leaves.
This module does what must survive the exit from aboutToQuit instead (measured on Qt 6.11, 2026-11-27).

cockpit/ never calls quit() or exit() on the application, so aboutToQuit with the window still open comes only
from Windows or an external WM_QUIT. commitDataRequest arms the handler for SESSION_END_VALIDITY_MS, which covers
a shutdown another program holds up while the operator answers Windows; a cancelled shutdown leaves a stale flag
for at most that long.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from cockpit.ui.collector import monotonic_ms
from cockpit.ui.process_exit import (
    EXIT_LINE_PREFIX,
    ExitFlushables,
    ExitReason,
    ExitRecord,
    flush_owned_clipboard,
)

logger = logging.getLogger(__name__)

SESSION_END_VALIDITY_MS = 600_000
SESSION_END_CODE = "na"


@dataclass
class SessionEnd:
    """The latest commitDataRequest, which arms end_session_now for SESSION_END_VALIDITY_MS."""

    request_seen_ms: Optional[int] = None

    def armed(self, now_ms: int) -> bool:
        seen = self.request_seen_ms
        return seen is not None and 0 <= now_ms - seen <= SESSION_END_VALIDITY_MS


@dataclass
class SessionEndHandle:
    """The two QApplication connections install_session_end makes; dispose() disconnects both."""

    _disconnect: Callable[[], None]
    _disposed: bool = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._disconnect()


def _flush_root_handlers() -> None:
    for handler in list(logging.getLogger().handlers):
        try:
            handler.flush()
        except Exception:
            pass


def end_session_now(
    window,
    flushables: ExitFlushables,
    health,
    retention,
    exit_record: ExitRecord,
) -> None:
    """Records a Windows session end before Windows ends the process (Patch 15 §4.2).

    pre:  GUI thread, from aboutToQuit; armed by commitDataRequest; the window isn't closed; the exit isn't logged
    post: in order, each step isolated (a failing step is logged at ERROR and the next one still runs):
          1. window.begin_session_end(), and the health logger's timer stops;
          2. the health line with at=session_end (when health exists) and
             "Exit: code=na; reason=session_end; retained threads: …" are logged, and every root handler is
             flushed (nothing is closed; logging.shutdown doesn't run);
          3. the settings are synced;
          4. the clipboard is flushed when Cockpit owns it;
          5. window.close_connection_for_session_end();
          6. the fault file is flushed;
          7. exit_record becomes (logged = true, reason = SESSION_END).
          No thread is stopped or waited on, no dialog closed and no view unloaded.
    raises: nothing
    """

    def step(name: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception:
            logger.exception("Session-end step %r failed", name)

    def stop_activity() -> None:
        window.begin_session_end()
        if health is not None:
            health.stop()

    def log_exit() -> None:
        if health is not None:
            from cockpit.ui.health import HealthReason

            health.log_now(HealthReason.SESSION_END)
        describe = retention.describe_outstanding if retention is not None else (lambda: "none")
        logger.info(
            "%s%s; reason=%s; retained threads: %s",
            EXIT_LINE_PREFIX, SESSION_END_CODE, ExitReason.SESSION_END.value, describe(),
        )
        _flush_root_handlers()

    step("stop activity", stop_activity)
    step("exit line", log_exit)
    if flushables.settings is not None:
        step("settings sync", flushables.settings.sync)
    step("clipboard", flush_owned_clipboard)
    step("connection", window.close_connection_for_session_end)
    if flushables.fault_file is not None:
        step("fault file", flushables.fault_file.flush)
    exit_record.logged, exit_record.reason = True, ExitReason.SESSION_END
    _flush_root_handlers()


def install_session_end(
    app,
    window,
    flushables: ExitFlushables,
    health: Callable[[], object],
    retention,
    exit_record: ExitRecord,
    session: SessionEnd,
    clock: Callable[[], int] = monotonic_ms,
) -> SessionEndHandle:
    """Connects the session-end handling to the application (Patch 15 §4.2).

    pre:  GUI thread; window constructed
    post: commitDataRequest sets session.request_seen_ms and syncs the settings, nothing else (another program
          can still cancel the shutdown). aboutToQuit calls end_session_now when the session is armed, the window
          isn't closed and the exit isn't logged. The handle's dispose() disconnects both
    raises: nothing
    """

    def on_commit_data(manager=None) -> None:
        session.request_seen_ms = clock()
        logger.info("Windows asked to end the session")
        if flushables.settings is not None:
            try:
                flushables.settings.sync()
            except Exception:
                logger.exception("Settings sync on the session-end request failed")

    def on_about_to_quit() -> None:
        if not session.armed(clock()) or exit_record.logged or window.is_closed():
            return
        end_session_now(window, flushables, health(), retention, exit_record)

    app.commitDataRequest.connect(on_commit_data)
    app.aboutToQuit.connect(on_about_to_quit)

    def disconnect() -> None:
        for signal, slot in ((app.commitDataRequest, on_commit_data), (app.aboutToQuit, on_about_to_quit)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    return SessionEndHandle(disconnect)
