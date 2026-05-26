"""Crash reporting infrastructure."""

import dataclasses
import datetime
import json
import logging
import os
import pathlib
import sys
import threading
import traceback
from typing import Protocol

from cockpit._build_info import BuildInfo
from cockpit.ui.runtime import runtime_kind


@dataclasses.dataclass(frozen=True)
class CrashReport:
    schema_version: int
    occurred_at_utc: str
    app_version: str
    build_commit: str
    runtime_kind: str
    python_version: str
    qt_version: str
    exception_class: str
    exception_message: str
    traceback_lines: tuple[str, ...]
    thread_name: str
    reason_code: str | None


class CrashSink(Protocol):
    def emit(self, report: CrashReport) -> None:
        ...


class LocalFileCrashSink:
    def __init__(self, crash_dir: pathlib.Path):
        self.crash_dir = crash_dir

    def emit(self, report: CrashReport) -> None:
        if os.environ.get("COCKPIT_DISABLE_CRASH_REPORTS") == "1":
            return
            
        try:
            self.crash_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
            
        safe_exc = "".join(c if c.isalnum() else "_" for c in report.exception_class)
        ts = report.occurred_at_utc.replace(":", "-")
        filename = f"{ts}-{safe_exc}.json"
        
        path = self.crash_dir / filename
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(dataclasses.asdict(report), f, indent=2)
                
            self._prune()
        except OSError:
            pass

    def _prune(self) -> None:
        try:
            files = list(self.crash_dir.glob("*.json"))
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[50:]:
                p.unlink(missing_ok=True)
        except OSError:
            pass


class StderrCrashSink:
    def emit(self, report: CrashReport) -> None:
        msg = f"CRASH: {report.exception_class} ({report.reason_code or 'UNKNOWN'}) - {report.exception_message}\n"
        sys.stderr.write(msg)


_installed_chain = None


def install_crash_reporter(crash_dir: pathlib.Path, build_info: BuildInfo, sinks: tuple[CrashSink, ...]) -> None:
    """Install the crash reporter into sys.excepthook and Qt message handlers.
    
    Can be called multiple times; subsequent calls replace the active sinks while
    preserving the original sys.excepthook chain.
    """
    global _installed_chain
    
    if _installed_chain is None:
        _installed_chain = sys.excepthook
        
    try:
        from PyQt6.QtCore import QT_VERSION_STR
        qt_version = QT_VERSION_STR
    except ImportError:
        qt_version = "unknown"

    def _handle_exception(exc_type, exc_value, exc_traceback):
        try:
            tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
            reason_code = getattr(exc_value, "reason_code", None)
            
            report = CrashReport(
                schema_version=1,
                occurred_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                app_version=build_info.version,
                build_commit=build_info.commit,
                runtime_kind=runtime_kind().value,
                python_version=sys.version.split()[0],
                qt_version=qt_version,
                exception_class=exc_type.__name__,
                exception_message=str(exc_value),
                traceback_lines=tuple(tb_lines),
                thread_name=threading.current_thread().name,
                reason_code=reason_code
            )
            
            for sink in sinks:
                try:
                    sink.emit(report)
                except Exception:
                    sys.stderr.write(f"CrashSink {sink} failed to emit.\n")
        except Exception:
            sys.stderr.write("Crash reporter failed.\n")
            
        # Always chain to the original hook
        if _installed_chain is not None:
            _installed_chain(exc_type, exc_value, exc_traceback)
            
    sys.excepthook = _handle_exception
    
    try:
        from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
        def _qt_message_handler(msg_type, context, msg):
            qt_logger = logging.getLogger("cockpit.qt")
            if msg_type == QtMsgType.QtFatalMsg:
                try:
                    report = CrashReport(
                        schema_version=1,
                        occurred_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        app_version=build_info.version,
                        build_commit=build_info.commit,
                        runtime_kind=runtime_kind().value,
                        python_version=sys.version.split()[0],
                        qt_version=qt_version,
                        exception_class="QtFatalMsg",
                        exception_message=msg,
                        traceback_lines=("Qt Fatal Message", f"File: {context.file}", f"Line: {context.line}", f"Function: {context.function}"),
                        thread_name=threading.current_thread().name,
                        reason_code="QT_FATAL"
                    )
                    for sink in sinks:
                        try:
                            sink.emit(report)
                        except Exception:
                            sys.stderr.write(f"CrashSink {sink} failed to emit.\n")
                except Exception:
                    sys.stderr.write("Crash reporter failed in Qt handler.\n")
            elif msg_type == QtMsgType.QtCriticalMsg:
                qt_logger.error("Qt critical at %s:%s in %s — %s", context.file, context.line, context.function, msg)
            elif msg_type == QtMsgType.QtWarningMsg:
                qt_logger.warning("Qt warning at %s:%s in %s — %s", context.file, context.line, context.function, msg)
            elif msg_type == QtMsgType.QtInfoMsg:
                qt_logger.info("Qt info: %s", msg)
            elif msg_type == QtMsgType.QtDebugMsg:
                qt_logger.debug("Qt debug: %s", msg)
        qInstallMessageHandler(_qt_message_handler)
    except ImportError:
        pass
