"""Summarizes a folder of Cockpit logs without printing their content (Patch 14 §10.3).

Usage:  python -I scripts/audit/log_census.py <folder>

The folder holds cockpit.log* (the rotating log), and optionally faulthandler.log and crash_reports/. A
production copy keeps all three at the data root and under v1/; both layouts are read. Every path, quoted
string and number inside a message is masked, so the report can be pasted into a plan without customer
names. The script holds no data of its own.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import json
import pathlib
import re
import sys
from typing import Iterable, Iterator, Optional

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - (.*?) - (\S+) - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - (.*)$")
TRACEBACK_TAIL = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)\w*)(?::\s(.*))?$")
HEALTH_PREFIX = "health:"
EXIT_PREFIX = "Exit: code="
START_MARK = "Bootstrapping Cockpit application"
RESTING = {"page": "list", "busy": "none"}
WARM_UP_HOURS = 24.0


def mask(text: str) -> str:
    """Paths, quoted strings and numbers replaced, so no customer text survives."""
    text = re.sub(r"[A-Za-z]:[\\/][^\s'\"]*|\\\\[^\s'\"]+|(?<!\w)/[\w./-]+", "<path>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "<q>", text)
    return re.sub(r"\d+(?:\.\d+)?", "#", text)[:160]


def rotation_order(folder: pathlib.Path) -> list[pathlib.Path]:
    """cockpit.log.N from the highest N down, then cockpit.log: oldest first."""
    def rank(path: pathlib.Path) -> int:
        suffix = path.name.rsplit(".", 1)[-1]
        return -int(suffix) if suffix.isdigit() else 0
    return sorted(folder.glob("cockpit.log*"), key=rank)


def find_logs(folder: pathlib.Path) -> list[pathlib.Path]:
    for candidate in (folder, folder / "logs", folder / "v1" / "logs"):
        found = rotation_order(candidate)
        if found:
            return found
    return []


@dataclasses.dataclass
class Session:
    started: str
    build: str = "unknown"
    ended: str = ""
    ended_cleanly: bool = False
    health: list[tuple[datetime.datetime, dict[str, str]]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Trend:
    first: float
    last: float
    maximum: float
    per_day: Optional[float]


@dataclasses.dataclass
class CensusReport:
    sessions: list[Session] = dataclasses.field(default_factory=list)
    qt_warnings: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    warnings_by_template: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    exceptions: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    faulthandler_entries: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    crash_reports: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    unreadable_lines: int = 0


def parse_health(message: str) -> dict[str, str]:
    body = message[len(HEALTH_PREFIX):].strip()
    return dict(pair.split("=", 1) for pair in body.split() if "=" in pair)


def least_squares_per_day(points: list[tuple[float, float]]) -> Optional[float]:
    """Slope of value over time (days), or None with fewer than two points."""
    if len(points) < 2:
        return None
    n = float(len(points))
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def health_trends(session: Session) -> dict[str, Trend]:
    """first, last, maximum and growth per day for each numeric key, over resting readings after 24 h."""
    trends: dict[str, Trend] = {}
    if not session.health:
        return trends
    start = session.health[0][0]
    keys = [key for key in session.health[0][1] if key not in ("at", "page", "busy")]
    for key in keys:
        values = [(when, values.get(key)) for when, values in session.health]
        numbers = [(when, float(v)) for when, v in values if v not in (None, "na")]
        if not numbers:
            continue
        resting = [
            ((when - start).total_seconds() / 86_400.0, float(values[key]))
            for when, values in session.health
            if all(values.get(k) == v for k, v in RESTING.items())
            and (when - start).total_seconds() / 3600.0 >= WARM_UP_HOURS
            and values.get(key) not in (None, "na")
        ]
        trends[key] = Trend(
            first=numbers[0][1], last=numbers[-1][1], maximum=max(v for _, v in numbers),
            per_day=least_squares_per_day(resting),
        )
    return trends


def read_lines(path: pathlib.Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            yield raw.rstrip("\n")


def census(folder: pathlib.Path) -> CensusReport:
    """Summarizes a folder of Cockpit logs without printing their content.

    pre:  folder holds cockpit.log*, and optionally faulthandler.log and crash_reports/
    post: rotations are read oldest first; a session starts at its Bootstrapping line and ends at its exit
          line, or, without one, at its last line before the next start or the end of the logs
          (ended_cleanly false); everything counted is masked
    raises: OSError when folder can't be read; an unreadable line is skipped and counted
    """
    report = CensusReport()
    pending_build = "unknown"
    session: Optional[Session] = None
    head: Optional[tuple[str, str]] = None
    frame = "?"
    for path in find_logs(folder):
        for line in read_lines(path):
            if "\ufffd" in line:
                report.unreadable_lines += 1
            match = LINE.match(line)
            if match is None:
                stripped = line.strip()
                if stripped.startswith('File "'):
                    frame = re.sub(r"line \d+", "line #", stripped.replace("\\", "/").split("/")[-1])
                    continue
                tail = TRACEBACK_TAIL.match(stripped)
                if tail and head is not None:
                    report.exceptions[(tail.group(1), head[0], frame)] += 1
                    head = None
                continue
            when, thread, logger_name, level, message = match.groups()
            if session is not None:
                session.ended = when
            if message.startswith("Build info:"):
                pending_build = message.split(",")[0].replace("Build info: ", "")
            if START_MARK in message:
                session = Session(started=when, build=pending_build, ended=when)
                report.sessions.append(session)
                pending_build = "unknown"
            elif message.startswith(EXIT_PREFIX) and session is not None:
                session.ended_cleanly = True
            elif message.startswith(HEALTH_PREFIX) and session is not None:
                stamp = datetime.datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
                session.health.append((stamp, parse_health(message)))
            if logger_name == "cockpit.qt":
                report.qt_warnings[mask(message.split(" — ", 1)[-1])] += 1
            elif level in ("WARNING", "ERROR", "CRITICAL"):
                report.warnings_by_template[(level, logger_name, mask(message))] += 1
                head = (logger_name, mask(message))
                frame = "?"
    for fault_log in (folder / "faulthandler.log", folder.parent / "faulthandler.log"):
        if fault_log.exists():
            for line in read_lines(fault_log):
                if line.startswith("Windows fatal exception:") or line.startswith("Fatal Python error:"):
                    report.faulthandler_entries[mask(line)] += 1
            break
    for reports in (folder / "crash_reports", folder / "v1" / "crash_reports"):
        if reports.is_dir():
            for crash in reports.glob("*.json"):
                try:
                    data = json.loads(crash.read_text(encoding="utf-8"))
                    report.crash_reports[str(data.get("exception_class", "?"))] += 1
                except (OSError, ValueError):
                    report.unreadable_lines += 1
            break
    return report


def render(report: CensusReport) -> Iterable[str]:
    yield f"sessions: {len(report.sessions)}"
    for session in report.sessions:
        state = "clean exit" if session.ended_cleanly else "no exit line"
        yield f"  {session.started} -> {session.ended}  {session.build}  ({state}, {len(session.health)} health lines)"
        for key, trend in health_trends(session).items():
            slope = "n/a" if trend.per_day is None else f"{trend.per_day:+.2f}/day"
            yield f"      {key:<18} first {trend.first:g}  last {trend.last:g}  max {trend.maximum:g}  resting {slope}"
    yield "Qt messages by class:"
    for text, count in report.qt_warnings.most_common(25):
        yield f"  {count:>8}  {text}"
    yield "WARNING and above by template:"
    for (level, logger_name, text), count in report.warnings_by_template.most_common(40):
        yield f"  {count:>8}  {level:<8} {logger_name:<40} {text}"
    yield "Logged exceptions by class and innermost frame:"
    for (exc, logger_name, frame), count in report.exceptions.most_common(25):
        yield f"  {count:>8}  {exc}  ({logger_name}; {frame})"
    yield "faulthandler entries:"
    for text, count in report.faulthandler_entries.most_common():
        yield f"  {count:>8}  {text}"
    yield "crash reports by class:"
    for text, count in report.crash_reports.most_common():
        yield f"  {count:>8}  {text}"
    yield f"unreadable lines: {report.unreadable_lines}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    for line in render(census(pathlib.Path(argv[1]))):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
