"""Summarizes a folder of Cockpit logs without printing their content (Patch 14 §10.3; Patch 15 §9).

Usage:  python -I scripts/audit/log_census.py <folder>

The folder holds cockpit.log* (the rotating log), and optionally faulthandler.log and crash_reports/. A
production copy keeps all three at the data root and under v1/; both layouts are read. Every path, quoted
string and number inside a message is masked, so the report can be pasted into a plan without customer
names. The script holds no data of its own.

Patch 15 §9 (F27): every count belongs to one session, or to a named bucket outside them, so history from an
older build can't read as current.
- A session opens at its "Session start:" line (Patch 15 builds) or, in older logs, at its "Bootstrapping"
  line; there, lines logged more than 5 s after the previous session's exit line belong to the next session.
- faulthandler.log entries follow the session marker before them, matched by pid and the nearest start within
  60 s; crash reports go by their JSON occurred_at_utc in local time (their modification time as a fallback).
- Per session: the exit reason, the growth fit against the operation counters, and the post-ingest probe chain.
- Flags: a session without an exit line, and a newest log whose last byte isn't a newline (an incomplete copy).
"""

from __future__ import annotations

import bisect
import collections
import dataclasses
import datetime
import json
import pathlib
import re
import statistics
import sys
from typing import Iterable, Iterator, Optional

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - (.*?) - (\S+) - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - (.*)$")
TRACEBACK_TAIL = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)\w*)(?::\s(.*))?$")
HEALTH_PREFIX = "health:"
PROBE_PREFIX = "probe:"
EXIT_PREFIX = "Exit: code="
START_MARK = "Bootstrapping Cockpit application"
SESSION_START = "Session start:"
FAULT_MARKER = re.compile(r"^--- session (\S+) pid=(\d+) version=(\S*) ---$")
RESTING = {"page": "list", "busy": "none"}
WARM_UP_HOURS = 24.0
EXIT_GRACE_S = 5.0
MARKER_WINDOW_S = 60.0
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
BEFORE_FIRST = "before the first session in these files"
UNDATED = "undated faulthandler entries (before any session marker)"
UNMATCHED_MARKER = "unmatched marker (no logged session with its pid near its time)"
OUTSIDE = "outside the logged sessions"
COUNTERS = ("n_ingest", "n_open", "n_update_run")


def mask(text: str) -> str:
    """Paths, quoted strings and numbers replaced, so no customer text survives."""
    text = re.sub(r"[A-Za-z]:[\\/][^\s'\"]*|\\\\[^\s'\"]+|(?<!\w)/[\w./-]+", "<path>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "<q>", text)
    return re.sub(r"\d+(?:\.\d+)?", "#", text)[:160]


def rotation_order(folder: pathlib.Path) -> list[pathlib.Path]:
    """Oldest first: 1.84's cockpit.log.N from the highest N down, then the dated weekly files in date order
    (Patch 15 §8), then cockpit.log."""
    def rank(path: pathlib.Path) -> tuple:
        suffix = path.name[len("cockpit.log"):].lstrip(".")
        if not suffix:
            return (2, "")
        if suffix.isdigit():
            return (0, -int(suffix))
        return (1, suffix)
    found = [p for p in folder.glob("cockpit.log*") if p.is_file()]
    numbered = sorted((p for p in found if rank(p)[0] == 0), key=lambda p: rank(p)[1])
    dated = sorted((p for p in found if rank(p)[0] == 1), key=lambda p: rank(p)[1])
    live = [p for p in found if rank(p)[0] == 2]
    return numbered + dated + live


def find_logs(folder: pathlib.Path) -> list[pathlib.Path]:
    for candidate in (folder, folder / "logs", folder / "v1" / "logs"):
        found = rotation_order(candidate)
        if found:
            return found
    return []


@dataclasses.dataclass
class Counts:
    """What one session, or one outside bucket, logged."""

    qt_warnings: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    warnings_by_template: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    exceptions: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    faulthandler_entries: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    crash_reports: collections.Counter = dataclasses.field(default_factory=collections.Counter)

    def empty(self) -> bool:
        return not any((self.qt_warnings, self.warnings_by_template, self.exceptions,
                        self.faulthandler_entries, self.crash_reports))


@dataclasses.dataclass
class Session:
    started: str
    build: str = "unknown"
    ended: str = ""
    ended_cleanly: bool = False
    exit_reason: Optional[str] = None
    pid: Optional[int] = None
    health: list[tuple[datetime.datetime, dict[str, str]]] = dataclasses.field(default_factory=list)
    probes: list[tuple[datetime.datetime, dict[str, object]]] = dataclasses.field(default_factory=list)
    counts: Counts = dataclasses.field(default_factory=Counts)


@dataclasses.dataclass
class Trend:
    first: float
    last: float
    maximum: float
    per_day: Optional[float]


@dataclasses.dataclass
class CensusReport:
    sessions: list[Session] = dataclasses.field(default_factory=list)
    buckets: dict[str, Counts] = dataclasses.field(default_factory=dict)
    # Totals over every session and bucket (Patch 14's view; Patch 15 §9 adds the attribution above).
    qt_warnings: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    warnings_by_template: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    exceptions: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    faulthandler_entries: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    crash_reports: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    unreadable_lines: int = 0
    truncated_last_line: bool = False

    def bucket(self, name: str) -> Counts:
        return self.buckets.setdefault(name, Counts())


def parse_health(message: str) -> dict[str, str]:
    body = message[len(HEALTH_PREFIX):].strip()
    return dict(pair.split("=", 1) for pair in body.split() if "=" in pair)


def parse_probe(message: str) -> dict[str, object]:
    """A probe line's fields (Patch 15 §3.2): four-point readings as lists, None for "na"."""
    fields: dict[str, object] = {}
    body = message[len(PROBE_PREFIX):] if message.startswith(PROBE_PREFIX) else message
    for pair in body.split():
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        try:
            if key in ("priv_mb", "ws_mb", "handles"):
                fields[key] = [None if part == "na" else float(part) for part in value.split("/")]
            elif key in ("n_ingest", "n_open", "n_update_run", "ms"):
                fields[key] = None if value == "na" else int(value)
            elif key in ("failed", "rises"):
                fields[key] = [] if value == "none" else value.split(",")
            else:
                fields[key] = value
        except ValueError:
            fields[key] = None
    return fields


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


def _resting(values: dict[str, str]) -> bool:
    return all(values.get(k) == v for k, v in RESTING.items())


def health_trends(session: Session) -> dict[str, Trend]:
    """first, last, maximum and growth per day for each numeric key, over resting readings after 24 h."""
    trends: dict[str, Trend] = {}
    if not session.health:
        return trends
    start = session.health[0][0]
    keys = [key for key in session.health[0][1] if key not in ("at", "page", "busy")]
    for key in keys:
        values = [(when, values.get(key)) for when, values in session.health]
        numbers = []
        for when, v in values:
            if v in (None, "na"):
                continue
            try:
                numbers.append((when, float(v)))
            except ValueError:
                continue
        if not numbers:
            continue
        resting = []
        for when, values in session.health:
            if not _resting(values) or (when - start).total_seconds() / 3600.0 < WARM_UP_HOURS:
                continue
            try:
                resting.append(((when - start).total_seconds() / 86_400.0, float(values[key])))
            except (KeyError, ValueError):
                continue
        trends[key] = Trend(
            first=numbers[0][1], last=numbers[-1][1], maximum=max(v for _, v in numbers),
            per_day=least_squares_per_day(resting),
        )
    return trends


def _solve(matrix: list[list[float]], vector: list[float]) -> Optional[list[float]]:
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(n):
            if r != col:
                factor = a[r][col] / a[col][col]
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def growth_fit(session: Session, key: str = "priv_mb") -> Optional[dict[str, object]]:
    """Patch 15 §9: Δkey = a·Δn_ingest + b·Δn_open + c·Δn_update_run over consecutive resting readings.

    Only windows where some counter rose, with every value present, take part; a counter that never rose is
    dropped from the fit. Returns the coefficients (None for a dropped counter) and the number of windows, or None
    when no window qualifies or the system is singular. Reported, not acceptance.
    """
    resting = [values for _, values in session.health if _resting(values)]
    rows: list[tuple[list[float], float]] = []
    for before, after in zip(resting, resting[1:]):
        try:
            deltas = [float(after[c]) - float(before[c]) for c in COUNTERS]
            change = float(after[key]) - float(before[key])
        except (KeyError, ValueError):
            continue
        if any(d > 0 for d in deltas):
            rows.append((deltas, change))
    if not rows:
        return None
    columns = [j for j in range(len(COUNTERS)) if any(r[0][j] for r in rows)]
    matrix = [[sum(r[0][i] * r[0][j] for r in rows) for j in columns] for i in columns]
    vector = [sum(r[0][i] * r[1] for r in rows) for i in columns]
    solution = _solve(matrix, vector)
    if solution is None:
        return None
    coefficients: dict[str, object] = {name: None for name in COUNTERS}
    for index, column in enumerate(columns):
        coefficients[COUNTERS[column]] = solution[index]
    coefficients["windows"] = len(rows)
    return coefficients


def probe_chain(session: Session) -> dict[str, object]:
    """Patch 15 §9: F21's estimator from consecutive valid probes.

    A probe is valid when its store and heap steps didn't fail and it carries n_ingest and the after-heaps
    priv_mb. For consecutive valid probes with Δn_ingest ≥ 1, the live growth per ingest is
    (p[i].after_heaps - p[i-1].after_heaps) / Δn_ingest. A failed probe breaks the chain; the next valid one compares
    with the last valid one.
    """
    probes = [fields for _, fields in session.probes]
    valid = []
    for fields in probes:
        failed = set(fields.get("failed") or [])
        priv = fields.get("priv_mb") or []
        if {"store", "heaps"} & failed or len(priv) != 4 or priv[3] is None or fields.get("n_ingest") is None:
            continue
        valid.append(fields)
    windows: list[tuple[float, int]] = []
    for previous, current in zip(valid, valid[1:]):
        ingests = current["n_ingest"] - previous["n_ingest"]
        if ingests >= 1:
            windows.append(((current["priv_mb"][3] - previous["priv_mb"][3]) / ingests, ingests))

    def release(index_from: int, index_to: int) -> Optional[float]:
        values = []
        for fields in probes:
            priv = fields.get("priv_mb") or []
            if len(priv) == 4 and priv[index_from] is not None and priv[index_to] is not None:
                values.append(priv[index_from] - priv[index_to])
        return statistics.median(values) if values else None

    rises: collections.Counter = collections.Counter()
    for fields in probes:
        for item in fields.get("rises") or []:
            rises[item.split(":", 1)[0]] += 1
    durations = [fields["ms"] for fields in probes if isinstance(fields.get("ms"), int)]
    return {
        "probes": len(probes),
        "valid": len(valid),
        "windows": len(windows),
        "ingests": sum(n for _, n in windows),
        "median_mb_per_ingest": statistics.median(g for g, _ in windows) if windows else None,
        "median_collection_release_mb": release(0, 1),
        "median_store_release_mb": release(1, 2),
        "median_heap_release_mb": release(2, 3),
        "max_ms": max(durations) if durations else None,
        "rising_classes": dict(rises),
    }


def read_lines(path: pathlib.Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            yield raw.rstrip("\n")


def _when(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, TIME_FORMAT)


def _exit_reason(message: str) -> str:
    match = re.search(r"reason=(\w+)", message)
    return match.group(1) if match else "quit"


@dataclasses.dataclass
class _Opening:
    start_index: int
    started: str
    build: str
    pid: Optional[int]
    by_start_line: bool
    seen_mark: bool = False
    exit_index: Optional[int] = None
    exit_time: Optional[datetime.datetime] = None
    after_grace_index: Optional[int] = None


def _all_lines(paths: Iterable[pathlib.Path]) -> Iterator[tuple[int, str]]:
    index = 0
    for path in paths:
        for line in read_lines(path):
            yield index, line
            index += 1


def _openings(paths: list[pathlib.Path]) -> list[_Opening]:
    """Pass 1: where each session's lines begin (Patch 15 §9's boundary rules)."""
    openings: list[_Opening] = []
    current: Optional[_Opening] = None
    pending_build = "unknown"
    for index, line in _all_lines(paths):
        match = LINE.match(line)
        if match is None:
            continue
        when, _thread, _logger, _level, message = match.groups()
        if current is not None and current.exit_time is not None and current.after_grace_index is None:
            if index > current.exit_index and (_when(when) - current.exit_time).total_seconds() > EXIT_GRACE_S:
                current.after_grace_index = index
        if message.startswith("Build info:"):
            build = message.split(",")[0].replace("Build info: ", "")
            if current is not None and current.by_start_line and not current.seen_mark and current.exit_time is None:
                current.build = build
            else:
                pending_build = build
        if message.startswith(SESSION_START):
            pid_match = re.search(r"pid=(\d+)", message)
            current = _Opening(index, when, "unknown", int(pid_match.group(1)) if pid_match else None, True)
            openings.append(current)
            pending_build = "unknown"
        elif START_MARK in message:
            if current is not None and current.by_start_line and not current.seen_mark and current.exit_time is None:
                current.seen_mark = True
            else:
                start = index
                if current is not None and current.exit_index is not None:
                    start = current.after_grace_index if current.after_grace_index is not None else index
                current = _Opening(start, when, pending_build, None, False, seen_mark=True)
                openings.append(current)
                pending_build = "unknown"
        elif message.startswith(EXIT_PREFIX) and current is not None and current.exit_time is None:
            current.exit_index = index
            current.exit_time = _when(when)
    return openings


def _fault_entries(path: pathlib.Path) -> Iterator[tuple[Optional[tuple[datetime.datetime, int]], str]]:
    """(marker, entry) for every fatal entry in a faulthandler log; marker is None before the first one."""
    marker: Optional[tuple[datetime.datetime, int]] = None
    for line in read_lines(path):
        found = FAULT_MARKER.match(line.strip())
        if found:
            try:
                marker = (datetime.datetime.fromisoformat(found.group(1)), int(found.group(2)))
            except ValueError:
                marker = None
            continue
        if line.startswith("Windows fatal exception:") or line.startswith("Fatal Python error:"):
            yield marker, mask(line)


def _session_for_marker(
    report: CensusReport, marker: tuple[datetime.datetime, int]
) -> Optional[Session]:
    moment, pid = marker
    best, best_gap = None, None
    for session in report.sessions:
        if session.pid != pid:
            continue
        gap = abs((_when(session.started) - moment).total_seconds())
        if gap <= MARKER_WINDOW_S and (best_gap is None or gap < best_gap):
            best, best_gap = session, gap
    return best


def _crash_time(path: pathlib.Path, data: Optional[dict]) -> Optional[datetime.datetime]:
    """occurred_at_utc in local time, or the file's modification time when the JSON has none."""
    raw = (data or {}).get("occurred_at_utc")
    if isinstance(raw, str):
        try:
            moment = datetime.datetime.fromisoformat(raw)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            return moment.astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _session_at(report: CensusReport, moment: Optional[datetime.datetime]) -> Optional[Session]:
    if moment is None:
        return None
    for session in report.sessions:
        start = _when(session.started)
        end = _when(session.ended) if session.ended else start
        if start <= moment <= end:
            return session
    return None


def census(folder: pathlib.Path) -> CensusReport:
    """Summarizes a folder of Cockpit logs without printing their content (Patch 15 §9).

    pre:  folder holds cockpit.log*, and optionally faulthandler.log and crash_reports/
    post: sessions as described in the module docstring, each with its exit reason; every Qt message,
          WARNING-or-above line, logged exception, fault-log entry and crash report counted once, under exactly one
          session or one named bucket, and in the totals; the truncation flag; everything counted is masked
    raises: OSError when folder can't be read; an unreadable line is skipped and counted
    """
    report = CensusReport()
    paths = find_logs(folder)
    openings = _openings(paths)
    starts = [opening.start_index for opening in openings]
    for opening in openings:
        session = Session(started=opening.started, build=opening.build, ended=opening.started, pid=opening.pid)
        report.sessions.append(session)

    head: Optional[tuple[str, str]] = None
    frame = "?"
    target: Counts = report.bucket(BEFORE_FIRST)
    session: Optional[Session] = None
    for index, line in _all_lines(paths):
        position = bisect.bisect_right(starts, index) - 1
        if position >= 0:
            session = report.sessions[position]
            target = session.counts
        else:
            session = None
            target = report.bucket(BEFORE_FIRST)
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
                key = (tail.group(1), head[0], frame)
                target.exceptions[key] += 1
                report.exceptions[key] += 1
                head = None
            continue
        when, thread, logger_name, level, message = match.groups()
        if session is not None:
            session.ended = when
            if message.startswith(EXIT_PREFIX) and not session.ended_cleanly:
                session.ended_cleanly = True
                session.exit_reason = _exit_reason(message)
            elif message.startswith(HEALTH_PREFIX):
                session.health.append((_when(when), parse_health(message)))
            elif message.startswith(PROBE_PREFIX):
                session.probes.append((_when(when), parse_probe(message)))
        if logger_name == "cockpit.qt":
            key = mask(message.split(" — ", 1)[-1])
            target.qt_warnings[key] += 1
            report.qt_warnings[key] += 1
        elif level in ("WARNING", "ERROR", "CRITICAL"):
            key = (level, logger_name, mask(message))
            target.warnings_by_template[key] += 1
            report.warnings_by_template[key] += 1
            head = (logger_name, mask(message))
            frame = "?"

    if paths:
        try:
            with paths[-1].open("rb") as handle:
                handle.seek(0, 2)
                if handle.tell() > 0:
                    handle.seek(-1, 2)
                    report.truncated_last_line = handle.read(1) != b"\n"
        except OSError:
            pass

    for fault_log in (folder / "faulthandler.log", folder.parent / "faulthandler.log"):
        if fault_log.exists():
            for marker, entry in _fault_entries(fault_log):
                report.faulthandler_entries[entry] += 1
                if marker is None:
                    report.bucket(UNDATED).faulthandler_entries[entry] += 1
                    continue
                owner = _session_for_marker(report, marker)
                counts = owner.counts if owner is not None else report.bucket(UNMATCHED_MARKER)
                counts.faulthandler_entries[entry] += 1
            break
    for reports in (folder / "crash_reports", folder / "v1" / "crash_reports"):
        if reports.is_dir():
            for crash in reports.glob("*.json"):
                try:
                    data = json.loads(crash.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report.unreadable_lines += 1
                    data = None
                kind = str((data or {}).get("exception_class", "?"))
                report.crash_reports[kind] += 1
                owner = _session_at(report, _crash_time(crash, data))
                counts = owner.counts if owner is not None else report.bucket(OUTSIDE)
                counts.crash_reports[kind] += 1
            break
    return report


def _render_counts(counts: Counts, indent: str = "    ") -> Iterator[str]:
    for title, counter, limit in (
        ("Qt messages", counts.qt_warnings, 15),
        ("WARNING and above", counts.warnings_by_template, 20),
        ("logged exceptions", counts.exceptions, 15),
        ("faulthandler entries", counts.faulthandler_entries, 10),
        ("crash reports", counts.crash_reports, 10),
    ):
        if not counter:
            continue
        yield f"{indent}{title}:"
        for key, count in counter.most_common(limit):
            text = " ".join(str(part) for part in key) if isinstance(key, tuple) else str(key)
            yield f"{indent}  {count:>8}  {text}"


def _number(value: object, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}" if isinstance(value, float) else str(value)


def render(report: CensusReport) -> Iterable[str]:
    yield f"sessions: {len(report.sessions)}"
    for session in report.sessions:
        if session.ended_cleanly:
            state = f"exit: {session.exit_reason or 'quit'}"
        else:
            state = "no exit line (crash, kill, Windows session end before 1.85, or an incomplete copy)"
        pid = f" pid={session.pid}" if session.pid is not None else ""
        yield (f"  {session.started} -> {session.ended}  {session.build}{pid}  ({state}, "
               f"{len(session.health)} health lines, {len(session.probes)} probes)")
        for key, trend in health_trends(session).items():
            slope = "n/a" if trend.per_day is None else f"{trend.per_day:+.2f}/day"
            yield f"      {key:<18} first {trend.first:g}  last {trend.last:g}  max {trend.maximum:g}  resting {slope}"
        for key in ("priv_mb", "gdi", "handles"):
            fit = growth_fit(session, key)
            if fit is not None:
                yield (f"      growth fit {key}: per ingest {_number(fit['n_ingest'])}, per open {_number(fit['n_open'])}, "
                       f"per update run {_number(fit['n_update_run'])} over {fit['windows']} windows")
        if session.probes:
            chain = probe_chain(session)
            yield (f"      probe chain: {chain['valid']}/{chain['probes']} valid, {chain['windows']} windows, "
                   f"{chain['ingests']} ingests, median live growth {_number(chain['median_mb_per_ingest'])} MB per ingest; "
                   f"median release: collection {_number(chain['median_collection_release_mb'])}, "
                   f"store {_number(chain['median_store_release_mb'])}, heaps {_number(chain['median_heap_release_mb'])} MB; "
                   f"longest probe {chain['max_ms']} ms")
            if chain["rising_classes"]:
                yield f"      probe rises: {chain['rising_classes']}"
        yield from _render_counts(session.counts, "    ")
    for name, counts in report.buckets.items():
        if counts.empty():
            continue
        yield f"  {name}:"
        yield from _render_counts(counts, "    ")
    if report.truncated_last_line:
        yield "flag: the newest log's last line is truncated: incomplete copy?"
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
