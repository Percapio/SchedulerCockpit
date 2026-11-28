"""The primary log handler: weekly files kept by age, and every record it fails to write counted (Patch 15 §6, §8).

Python's own handlers report a failed write through Handler.handleError, which prints to sys.stderr when there is
one and otherwise drops it. A windowed build has no stderr, so a full disk or a locked log ended logging without
a trace (F24). This handler counts each failure instead; the health line carries the count (log_errors) and the
window shows a notice (Patch 15 §6).
"""

from __future__ import annotations

import logging
import logging.handlers
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

LOG_ROLLOVER_RETRY_S = 3_600
WEEKLY = "W0"


@dataclass(frozen=True)
class LoggingFaults:
    """What the primary handler failed to write since the process started."""

    count: int = 0
    first_error: Optional[str] = None
    first_at: Optional[float] = None


def retention_weeks(retention_days: int) -> int:
    """The number of weekly files that covers retention_days (at least one)."""
    return max(1, math.ceil(retention_days / 7))


class CountingLogHandler(logging.handlers.TimedRotatingFileHandler):
    """Writes log records to a weekly file and counts each record it fails to write (Patch 15 §6).

    pre:  built with delay=True (always, here), so the file opens on the first record, inside emit, and an
          unopenable log can't stop Cockpit starting
    post: handleError counts and never prints; doRollover survives an existing target and a failing rename;
          faults_snapshot() reads the counts under the handler's lock
    raises: nothing from emit
    """

    def __init__(
        self,
        filename: "os.PathLike[str] | str",
        retention_days: int,
        encoding: str = "utf-8",
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            filename, when=WEEKLY, backupCount=retention_weeks(retention_days), encoding=encoding, delay=True
        )
        self._clock = clock
        self._fault_count = 0
        self._first_error: Optional[str] = None
        self._first_at: Optional[float] = None

    # Handler.handle holds self.lock around emit, and emit is where handleError and doRollover run, so the
    # counters change under the lock.
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 (logging's name)
        self._record_fault(sys.exc_info()[1])

    def _record_fault(self, error: Optional[BaseException]) -> None:
        self._fault_count += 1
        if self._first_error is None:
            self._first_error = type(error).__name__ if error is not None else "unknown"
            self._first_at = self._clock()

    def faults_snapshot(self) -> LoggingFaults:
        with self.lock:
            return LoggingFaults(self._fault_count, self._first_error, self._first_at)

    def _dated_name(self, current_time: int) -> str:
        """The rotated file's name, for the interval that just ended (as TimedRotatingFileHandler names it)."""
        t = self.rolloverAt - self.interval
        if self.utc:
            time_tuple = time.gmtime(t)
        else:
            time_tuple = time.localtime(t)
            dst_now = time.localtime(current_time)[-1]
            dst_then = time_tuple[-1]
            if dst_now != dst_then:
                addend = 3600 if dst_now else -3600
                time_tuple = time.localtime(t + addend)
        return self.rotation_filename(self.baseFilename + "." + time.strftime(self.suffix, time_tuple))

    def doRollover(self) -> None:  # noqa: N802 (logging's name)
        """Rotates the live file, without the two failure modes of Python 3.12's version.

        - The dated target already exists: Python returns without moving rolloverAt and then retries on every
          record. Here rolloverAt moves to the next boundary and writing continues in the live file.
        - The rename raises (another program holds the file): Python leaves the stream closed and every later
          record is lost. Here one fault is counted, the live file is reopened on the next write, and the
          rollover is retried after LOG_ROLLOVER_RETRY_S.
        A failure deleting the oldest files is counted and doesn't stop the rotation.
        """
        current_time = int(time.time())
        target = self._dated_name(current_time)
        if os.path.exists(target):
            self.rolloverAt = self.computeRollover(current_time)
            return
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            self.rotate(self.baseFilename, target)
        except OSError as error:
            self._record_fault(error)
            self.rolloverAt = current_time + LOG_ROLLOVER_RETRY_S
            return
        if self.backupCount > 0:
            for old in self.getFilesToDelete():
                try:
                    os.remove(old)
                except OSError as error:
                    self._record_fault(error)
        self.rolloverAt = self.computeRollover(current_time)


_PRIMARY: Optional[CountingLogHandler] = None


def install_primary_handler(handler: Optional[CountingLogHandler]) -> None:
    """Names the handler whose faults the health line and the notice report (bootstrap calls it)."""
    global _PRIMARY
    _PRIMARY = handler


def logging_faults() -> LoggingFaults:
    """The primary handler's faults, or none when no primary handler is installed."""
    handler = _PRIMARY
    return handler.faults_snapshot() if handler is not None else LoggingFaults()
