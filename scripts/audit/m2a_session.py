"""Roadmap 02 M2a: what holds the memory each ingest keeps (F21), through the real app.main on a copy of
production data.

Usage, from the repository root, on the real display (GDI and USER counts need the "windows" platform plugin):
    python scripts/audit/m2a_session.py <production v1 folder> <work root> <summary.json> [--quick | --full]

--full runs M2 itself at its full counts (Roadmap 02 §4, Patch 15's AC2): 70 ingests over 7 families (10 rounds),
900 open/exit cycles round-robin over every open audit by BOM size, 50 each of the three report dialogs, 50 Settings,
7 update checks and 20 restyles, without the tracer, and evaluates M2's thresholds at the end.

The work root is deleted and rebuilt on every run, and holds customer data: keep it outside the repo and delete
it afterwards. It gets a fresh copy of the v1 folder, with stored paths repointed at the copy, and a fixture share
at <work>/share built from the 21 first-article families with the most stored bytes. The copy's settings point the source root at
that share and switch the MPN library off, so nothing reaches the production share or DigiKey.

Blocks, each followed by a checkpoint on the Audit List after a DeferredDelete flush and a full collection:
    warm-up: 10 opens and one ingest;
    ingests: production's shape, through the fetch dialog, the duplicate question, the replacement confirmation,
             the ingest, the audit load and the automatic placements check, a checkpoint after each: first one
             ingest of each of 20 families new to this process, then the same 20 again;
    probes:  tracemalloc diff over each round, then MuPDF's store emptied, then every heap compacted;
    open/exit cycles: round-robin over the largest audits, then repeats of one audit;
    report dialogs (placements, cleaning, 2nd OPS), Settings, update checks (closed at the preview), restyles;
    probes again.
The summary holds every checkpoint, a reading at each step of every ingest, the tracer's top growth by line,
each probe's effect, the modal dialogs met and any harness error. Health lines are in the copy's cockpit.log.
"""
import argparse, collections, ctypes, gc, json, os, pathlib, shutil, sqlite3, sys, time, tracemalloc
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

parser = argparse.ArgumentParser()
parser.add_argument("source", type=pathlib.Path)
parser.add_argument("work", type=pathlib.Path)
parser.add_argument("summary", type=pathlib.Path)
parser.add_argument("--quick", action="store_true", help="a smoke run with tiny counts")
parser.add_argument("--full", action="store_true", help="M2 at its full counts, with its thresholds (AC2)")
args = parser.parse_args()

COUNTS = dict(jobs=20, cycles=100, same=20, reports=50, settings=50, checks=7, restyles=20)
if args.quick:
    COUNTS = dict(jobs=2, cycles=4, same=2, reports=2, settings=2, checks=1, restyles=2)
if args.full:
    COUNTS = dict(jobs=7, rounds=10, cycles=900, same=0, reports=50, settings=50, checks=7, restyles=20)

work = args.work
if work.exists():
    shutil.rmtree(work)
shutil.copytree(args.source, work / "v1", ignore=shutil.ignore_patterns("logs", "crash_reports"))
(work / "v1" / "logs").mkdir()

# The copied database stores absolute paths into the production PC's data folder; point them at the copy.
_db = sqlite3.connect(str(work / "v1" / "local_audit.db"))
_sample = _db.execute("SELECT local_storage_path FROM source_files WHERE local_storage_path LIKE '%uploads%' LIMIT 1").fetchone()
REPOINTED = 0
if _sample:
    _old = _sample[0][: _sample[0].lower().index("uploads") + len("uploads")]
    _new = str(work / "v1" / "uploads")
    for (_table,) in _db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for _column in [c[1] for c in _db.execute(f"PRAGMA table_info({_table})") if "path" in c[1].lower()]:
            REPOINTED += _db.execute(
                f"UPDATE {_table} SET {_column} = ? || substr({_column}, ?) WHERE {_column} LIKE ? || '%'",
                (_new, len(_old) + 1, _old),
            ).rowcount
    _db.commit()

# The fixture share: one job folder per first-article part number, for the families with the most stored bytes
# (one for the warm-up, then COUNTS["jobs"]), each holding the family's files under their original names.
_ranked = _db.execute(
    "SELECT a.part_number, a.id FROM active_audits a JOIN source_files s ON s.audit_id = a.id "
    "WHERE COALESCE(NULLIF(TRIM(a.article_revision), ''), 'FA') = 'FA' "
    "GROUP BY a.id ORDER BY SUM(COALESCE(s.source_size, 0)) DESC"
).fetchall()
JOBS: list[str] = []
SHARE = work / "share"
FIXTURE_FILES = collections.Counter()
for _part, _audit in _ranked:
    if _part in JOBS:
        continue
    _job_dir = SHARE / f"{_part[:4]}xxx" / f"{_part[:5]}xx" / _part
    _job_dir.mkdir(parents=True)
    for original, stored, category in _db.execute(
        "SELECT original_filename, local_storage_path, file_category FROM source_files WHERE audit_id = ?", (_audit,)
    ):
        shutil.copy2(stored, _job_dir / original)
        FIXTURE_FILES[category] += 1
    JOBS.append(_part)
    if len(JOBS) == COUNTS["jobs"] + 1:
        break
_db.close()

os.environ["COCKPIT_APP_DATA"] = str(work)
os.environ["COCKPIT_HEALTH_INTERVAL_MS"] = "60000"

from PyQt6.QtCore import QCoreApplication, QEvent, QSettings, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox
from PyQt6 import sip

_settings = QSettings(str(work / "v1" / "settings.ini"), QSettings.Format.IniFormat)
_settings.setValue("ingestion/source_root", str(SHARE))
_settings.setValue("ingestion/last_job_number", JOBS[0])
_settings.setValue("library/enabled", False)
_settings.sync()
del _settings

import cockpit.ui.app as app_module
from cockpit.ui.config import ResolveOutcome

# F29: the default data root on this PC also holds a database, so resolve_app_data_root would refuse even with
# COCKPIT_APP_DATA set. The harness chooses the copy itself.
app_module.resolve_app_data_root = lambda: ResolveOutcome(chosen_root=work, probe_history=())

from cockpit.ui.main_window import MainWindow
from cockpit.ui.process_counters import read_process_counters
from cockpit.layout.pdf_access import PdfStep, pdf_section

T0 = time.monotonic()
results: dict = {
    "counts": COUNTS, "paths_repointed": REPOINTED, "fixture_files": dict(FIXTURE_FILES), "jobs": len(JOBS),
    "checkpoints": [], "steps": [], "probes": {}, "tracemalloc_top": {}, "modals": collections.Counter(),
    "message_boxes": collections.Counter(), "ingests": [], "errors": [],
}
STATE = {"ingest": None, "job": None, "succeeded": 0, "failed": 0, "runs_finished": 0}
WINDOW: list = []


def counters() -> dict:
    c = read_process_counters()
    traced = tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0
    tracer = tracemalloc.get_tracemalloc_memory() if tracemalloc.is_tracing() else 0
    return {
        "t_s": round(time.monotonic() - T0, 1),
        "priv_mb": round(c.private_mb, 1),
        "priv_less_tracer_mb": round(c.private_mb - tracer / 2**20, 1),
        "ws_mb": round(c.working_set_mb, 1),
        "handles": c.handles, "gdi": c.gdi_objects, "user": c.user_objects,
        "py_blocks": sys.getallocatedblocks(),
        "traced_mb": round(traced / 2**20, 2),
        "tracer_mb": round(tracer / 2**20, 2),
    }


def raster_mb() -> float:
    try:
        canvas = WINDOW[0]._audit_view._layout_canvas
        return round(canvas._raster_cache.total_bytes() / 2**20, 1)
    except Exception:
        return -1.0


def wrapper_census() -> dict:
    found = collections.Counter(type(o).__name__ for o in gc.get_objects() if isinstance(o, sip.simplewrapper))
    return dict(found)


def checkpoint(label: str, census: bool = False) -> dict:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    gc.collect()
    tops = QApplication.topLevelWidgets()
    entry = {"label": label, **counters(), "widgets": len(QApplication.allWidgets()), "top": len(tops),
             "hidden_dialogs": sum(1 for w in tops if isinstance(w, QDialog) and not w.isVisible()),
             "raster_mb": raster_mb()}
    if census:
        entry["wrappers"] = wrapper_census()
    results["checkpoints"].append(entry)
    return entry


def step_reading(step: str) -> None:
    if STATE["ingest"] is not None:
        results["steps"].append({"ingest": STATE["ingest"], "step": step, **counters()})


def wrap(name: str, after=None) -> None:
    original = getattr(MainWindow, name)

    def wrapper(self, *a, **k):
        step_reading(f"{name}:enter")
        try:
            return original(self, *a, **k)
        finally:
            step_reading(f"{name}:exit")
            if after is not None:
                after()
    setattr(MainWindow, name, wrapper)


def _ingest_ok():
    STATE["succeeded"] += 1


def _run_done():
    STATE["runs_finished"] += 1


wrap("_on_fetch_succeeded")
wrap("_on_drop_received")
wrap("_on_ingest_succeeded", after=_ingest_ok)
wrap("_on_update_run_finished", after=_run_done)

DWELL_S = 1.0
_first_seen: dict = {}


def handle_modal() -> None:
    """Answers modal dialogs the way an operator who always confirms would, and closes everything else."""
    w = QApplication.activeModalWidget()
    if w is None:
        return
    name = type(w).__name__
    key = id(w)
    now = time.monotonic()
    if key not in _first_seen:
        _first_seen[key] = now
        results["modals"][name] += 1
        if isinstance(w, QMessageBox):
            results["message_boxes"][w.windowTitle()] += 1
    if name == "FetchJobDialog":
        if not w.property("m2a_fired"):
            w.setProperty("m2a_fired", True)
            w.edit.setText(STATE["job"])
            w.fetch_btn.click()
        elif now - _first_seen[key] > 120:
            results["errors"].append("fetch dialog still open after 120 s; rejected")
            w.reject()
        return
    if isinstance(w, QMessageBox) and w.windowTitle() == "Duplicate Part Number":
        w.button(QMessageBox.StandardButton.Yes).click()
        return
    if name in ("ReplaceConfirmDialog", "SelectSourcesDialog"):
        w.accept()
        return
    if now - _first_seen[key] < DWELL_S:
        return
    if isinstance(w, QMessageBox):
        button = w.button(QMessageBox.StandardButton.Ok) or w.button(QMessageBox.StandardButton.No) or w.defaultButton()
        if button is not None:
            button.click()
            return
    (w.reject if hasattr(w, "reject") else w.close)()


def close_modeless_dialogs() -> None:
    for w in QApplication.topLevelWidgets():
        if isinstance(w, QDialog) and w.isVisible() and not w.isModal():
            w.close()


def idle(window) -> bool:
    return window._busy.operation is None and getattr(window, "_update_run", None) is None and \
        QApplication.activeModalWidget() is None


def back_to_list(window):
    window._reload_list()
    yield ("sleep", 500)


def ingest_cycle(window, index: int, job: str):
    STATE["ingest"], STATE["job"] = index, job
    before_ok, before_runs = STATE["succeeded"], STATE["runs_finished"]
    started = time.monotonic()
    QTimer.singleShot(0, window._on_picker_fetch_job_requested)
    yield ("sleep", 1500)
    succeeded = yield ("until", lambda: STATE["succeeded"] > before_ok, 600_000, 500)
    if succeeded:
        yield ("until", lambda: STATE["runs_finished"] > before_runs, 120_000, 500)
        yield ("until", lambda: idle(window), 600_000, 500)
    results["ingests"].append({"index": index, "job": JOBS.index(job), "succeeded": bool(succeeded),
                               "placements_runs": STATE["runs_finished"] - before_runs,
                               "seconds": round(time.monotonic() - started, 1)})
    STATE["ingest"] = None
    yield from back_to_list(window)


def largest_audits(window, limit: int) -> list[int]:
    rows = window._bootstrapped.conn.execute(
        "SELECT a.id AS id, COUNT(c.id) AS n FROM active_audits a "
        "LEFT JOIN source_files s ON s.audit_id = a.id "
        "LEFT JOIN audit_bom_components c ON c.source_file_id = s.id "
        "GROUP BY a.id ORDER BY n DESC, a.id LIMIT ?", (limit,)
    ).fetchall()
    return [row["id"] for row in rows]


def most_placements(window, limit: int) -> list[int]:
    rows = window._bootstrapped.conn.execute(
        "SELECT a.id AS id, COUNT(p.id) AS n FROM active_audits a "
        "LEFT JOIN source_files s ON s.audit_id = a.id "
        "LEFT JOIN board_placements p ON p.placement_file_id = s.id "
        "GROUP BY a.id ORDER BY n DESC, a.id LIMIT ?", (limit,)
    ).fetchall()
    return [row["id"] for row in rows]


def open_cycle(window, audit_id: int):
    window._on_picker_audit_selected(audit_id)
    yield ("sleep", 3000)
    yield ("until", lambda: idle(window), 60_000, 250)
    window._reload_list()
    yield ("sleep", 500)


def probe_block(tag: str, before_snapshot):
    if before_snapshot is not None:
        after = tracemalloc.take_snapshot()
        stats = after.compare_to(before_snapshot, "lineno")[:30]
        results["tracemalloc_top"][tag] = [
            {"where": str(s.traceback), "size_diff_kb": round(s.size_diff / 1024, 1), "count_diff": s.count_diff}
            for s in stats
        ]
        del after
    readings = {"before": checkpoint(f"{tag}:before probes")}
    import fitz
    with pdf_section(PdfStep.CLOSE):
        fitz.TOOLS.store_shrink(100)
    yield ("sleep", 500)
    readings["after_store_empty"] = checkpoint(f"{tag}:after MuPDF store emptied")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetProcessHeaps.restype = ctypes.c_uint32
    kernel32.GetProcessHeaps.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    kernel32.HeapCompact.restype = ctypes.c_size_t
    kernel32.HeapCompact.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    count = kernel32.GetProcessHeaps(0, None)
    heaps = (ctypes.c_void_p * max(count, 1))()
    count = kernel32.GetProcessHeaps(count, heaps)
    for index in range(count):
        kernel32.HeapCompact(heaps[index], 0)
    yield ("sleep", 500)
    readings["after_heap_compact"] = checkpoint(f"{tag}:after heaps compacted")
    readings["heaps"] = count
    results["probes"][tag] = readings


def session(window):
    yield ("sleep", 2000)
    tracemalloc.start(1)
    checkpoint("start", census=True)
    for audit_id in largest_audits(window, 10):
        yield from open_cycle(window, audit_id)
    yield from ingest_cycle(window, 0, JOBS[0])
    checkpoint("warm-up", census=True)
    snapshots = {"warm-up": tracemalloc.take_snapshot()}
    index = 0
    for round_name in ("new jobs", "repeat jobs"):
        for job in JOBS[1:]:
            index += 1
            yield from ingest_cycle(window, index, job)
            checkpoint(f"ingest {index} ({round_name})")
        checkpoint(f"after {round_name}", census=True)
        snapshots[round_name] = tracemalloc.take_snapshot()
    for earlier, later in (("warm-up", "new jobs"), ("new jobs", "repeat jobs")):
        stats = snapshots[later].compare_to(snapshots[earlier], "lineno")[:30]
        results["tracemalloc_top"][f"{earlier} -> {later}"] = [
            {"where": str(s.traceback), "size_diff_kb": round(s.size_diff / 1024, 1), "count_diff": s.count_diff}
            for s in stats
        ]
    del snapshots, stats
    gc.collect()
    yield from probe_block("after ingests", None)

    audits = largest_audits(window, 25)
    for cycle in range(COUNTS["cycles"]):
        yield from open_cycle(window, audits[cycle % len(audits)])
        if (cycle + 1) % 20 == 0:
            checkpoint(f"open/exit {cycle + 1}", census=((cycle + 1) % 100 == 0))
    for repeat in range(COUNTS["same"]):
        yield from open_cycle(window, audits[0])
    checkpoint("same audit", census=True)

    targets = most_placements(window, 25)
    for label, opener in (("placements report", window._on_placements_report_requested),
                          ("cleaning report", window._on_audit_cleaning_report_requested),
                          ("2nd OPS review", window._on_audit_second_ops_requested)):
        for index in range(COUNTS["reports"]):
            opener(targets[index % len(targets)])
            yield ("sleep", 2500)
            yield ("until", lambda: idle(window), 60_000, 250)
            close_modeless_dialogs()
            yield ("sleep", 500)
        yield from back_to_list(window)
        checkpoint(label, census=True)

    for _ in range(COUNTS["settings"]):
        QTimer.singleShot(0, window._on_settings_requested)
        yield ("sleep", 1500)
        yield ("until", lambda: QApplication.activeModalWidget() is None, 30_000, 250)
    checkpoint("settings", census=True)

    for _ in range(COUNTS["checks"]):
        QTimer.singleShot(0, window._on_check_all_requested)
        yield ("sleep", 2000)
        yield ("until", lambda: idle(window), 600_000, 500)
        yield from back_to_list(window)
    checkpoint("update checks", census=True)

    for index in range(COUNTS["restyles"]):
        window._font_scale.request_delta(1 if index % 2 == 0 else -1)
        yield ("sleep", 800)
        yield ("until", lambda: not window._font_scale._restyle_timer.isActive(), 30_000, 100)
        yield ("sleep", 700)
    checkpoint("restyles", census=True)

    yield from probe_block("at the end", None)


def least_squares(points):
    n = len(points)
    if n < 2:
        return None
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in points) / den


def evaluate_m2() -> dict:
    """Roadmap 02 §4, M2's thresholds, each after its block's first checkpoint (the warm-up)."""
    by_label = {c["label"]: c for c in results["checkpoints"]}
    ingest_points = [c for c in results["checkpoints"] if c["label"].startswith("ingests ")]
    open_points = [c for c in results["checkpoints"] if c["label"].startswith("open/exit ")]
    verdicts = {}

    def per(points, key, unit_label, unit):
        if len(points) < 3:
            return None
        base = int(points[0]["label"].split()[1])
        return least_squares([((int(c["label"].split()[1]) - base) / unit, c[key]) for c in points[1:]])

    def check(name, value, limit):
        verdicts[name] = {"value": None if value is None else round(value, 2), "limit": limit,
                          "pass": value is not None and value <= limit}

    check("priv_mb per 10 ingests", per(ingest_points, "priv_mb", "ingests", 10), 10)
    check("gdi per 10 ingests", per(ingest_points, "gdi", "ingests", 10), 5)
    check("user per 10 ingests", per(ingest_points, "user", "ingests", 10), 5)
    check("handles per 10 ingests", per(ingest_points, "handles", "ingests", 10), 20)
    check("priv_mb per 100 open/exit cycles", per(open_points, "priv_mb", "cycles", 100), 5)
    warm = ingest_points[0] if ingest_points else None
    end = results["checkpoints"][-1]
    for key in ("widgets", "top", "hidden_dialogs"):
        if warm is not None:
            check(f"{key} growth", end[key] - warm[key], 0)
    if warm is not None and "wrappers" in warm and "wrappers" in end:
        worst = []
        for name in set(warm["wrappers"]) | set(end["wrappers"]):
            first = warm["wrappers"].get(name, 0)
            rise = end["wrappers"].get(name, 0) - first
            allowed = max(10, 0.01 * first)
            if rise > allowed:
                worst.append((name, rise, allowed))
        verdicts["live wrappers per class"] = {"value": worst, "limit": "max(10, 1%)", "pass": not worst}
    return verdicts


def probe_lines() -> list:
    """The app's own probe lines in the copy's log (Patch 15 §3.2), for AC2's pause bound."""
    from cockpit.ui.probe import parse_probe
    lines = []
    for path in sorted((work / "v1" / "logs").glob("cockpit.log*")):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if " - cockpit.probe - INFO - probe:" in raw:
                lines.append(parse_probe(raw.split(" - INFO - ", 1)[1]))
    return lines


def full_session(window):
    """M2 at its full counts (Roadmap 02 §4), for Patch 15's AC2."""
    yield ("sleep", 2000)
    checkpoint("start", census=True)
    for audit_id in largest_audits(window, 10):
        yield from open_cycle(window, audit_id)
    yield from ingest_cycle(window, 0, JOBS[0])
    checkpoint("warm-up", census=True)
    index = 0
    for _round in range(COUNTS["rounds"]):
        for job in JOBS[1:COUNTS["jobs"] + 1]:
            index += 1
            yield from ingest_cycle(window, index, job)
            if index % 10 == 0:
                checkpoint(f"ingests {index}", census=True)
    audits = largest_audits(window, 100_000)
    for cycle in range(COUNTS["cycles"]):
        yield from open_cycle(window, audits[cycle % len(audits)])
        if (cycle + 1) % 100 == 0:
            checkpoint(f"open/exit {cycle + 1}", census=True)
    targets = most_placements(window, 100_000)
    for label, opener in (("placements report", window._on_placements_report_requested),
                          ("cleaning report", window._on_audit_cleaning_report_requested),
                          ("2nd OPS review", window._on_audit_second_ops_requested)):
        for report_index in range(COUNTS["reports"]):
            opener(targets[report_index % len(targets)])
            yield ("sleep", 2500)
            yield ("until", lambda: idle(window), 60_000, 250)
            close_modeless_dialogs()
            yield ("sleep", 500)
        yield from back_to_list(window)
        checkpoint(label, census=True)
    for _ in range(COUNTS["settings"]):
        QTimer.singleShot(0, window._on_settings_requested)
        yield ("sleep", 1500)
        yield ("until", lambda: QApplication.activeModalWidget() is None, 30_000, 250)
    checkpoint("settings", census=True)
    for _ in range(COUNTS["checks"]):
        QTimer.singleShot(0, window._on_check_all_requested)
        yield ("sleep", 2000)
        yield ("until", lambda: idle(window), 600_000, 500)
        yield from back_to_list(window)
    checkpoint("update checks", census=True)
    for restyle in range(COUNTS["restyles"]):
        window._font_scale.request_delta(1 if restyle % 2 == 0 else -1)
        yield ("sleep", 800)
        yield ("until", lambda: not window._font_scale._restyle_timer.isActive(), 30_000, 100)
        yield ("sleep", 700)
    checkpoint("restyles", census=True)
    # Give the app's own probe tick (every 60 s, after 60 s without input) a chance at the end of the run.
    yield ("sleep", 130_000)
    results["thresholds"] = evaluate_m2()
    results["probe_lines"] = probe_lines()


def run(window, generator) -> None:
    def advance(value=None):
        try:
            command = generator.send(value)
        except StopIteration:
            finish(window)
            return
        except Exception as error:
            results["errors"].append(f"session: {error!r}")
            finish(window)
            return
        if command[0] == "sleep":
            QTimer.singleShot(command[1], advance)
        else:
            _, predicate, timeout_ms, poll_ms = command
            deadline = time.monotonic() + timeout_ms / 1000

            def poll():
                try:
                    ok = bool(predicate())
                except Exception:
                    ok = False
                if ok or time.monotonic() > deadline:
                    advance(ok)
                else:
                    QTimer.singleShot(poll_ms, poll)
            poll()
    advance(None)


def finish(window) -> None:
    results["modals"] = dict(results["modals"])
    results["message_boxes"] = dict(results["message_boxes"])
    results["elapsed_min"] = round((time.monotonic() - T0) / 60, 1)
    args.summary.write_text(json.dumps(results, indent=1), encoding="utf-8")
    tracemalloc.stop()
    window.close()


original_show = MainWindow.show


def show_and_drive(self):
    original_show(self)
    WINDOW.append(self)
    handler = QTimer(self)
    handler.timeout.connect(handle_modal)
    handler.start(250)
    QTimer.singleShot(3000, lambda: run(self, full_session(self) if args.full else session(self)))


MainWindow.show = show_and_drive
app_module.main()
