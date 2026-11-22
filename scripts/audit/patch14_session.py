"""Patch 14 AC3/AC5/AC7: a scripted session through the real app.main on a copy of production data.

Usage (offscreen, from the repository root):
    set QT_QPA_PLATFORM=offscreen
    python scripts/audit/patch14_session.py <production v1 folder> <work root> <corpus job folder> <summary.json>

The work root is deleted and replaced by a fresh copy of the v1 folder on every run; keep it outside the repo,
since it holds customer data, and delete it afterwards. Nothing reaches the production share (the copy's source
root is removed) or DigiKey (the MPN library is switched off). The copied database's stored-file paths are
repointed at the copy. The session: the 10 largest open audits (by BOM components) opened 5 times each, Settings
opened 10 times, one ingest of the corpus job (replacing its family if the copy has one), then one more minute.
It writes every GUI-thread collection's pause by trigger, the off-thread count, the gate counts and the PDF
figures to summary.json; the health lines and the exit line are in the copy's cockpit.log, for log_census.py.
M2 (Roadmap 02 §4) can grow from this.
"""
import gc, json, os, pathlib, shutil, statistics, sys, threading, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

source, work, job, summary_path = (pathlib.Path(a) for a in sys.argv[1:5])
if work.exists():
    shutil.rmtree(work)
shutil.copytree(source, work / "v1", ignore=shutil.ignore_patterns("logs", "crash_reports"))
(work / "v1" / "logs").mkdir()

# The copied database stores absolute paths into the production PC's own data folder; point them at the copy.
import sqlite3 as _sqlite3
_db = _sqlite3.connect(str(work / "v1" / "local_audit.db"))
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
_db.close()
os.environ["COCKPIT_APP_DATA"] = str(work)
os.environ["COCKPIT_HEALTH_INTERVAL_MS"] = "60000"

from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QApplication
settings = QSettings(str(work / "v1" / "settings.ini"), QSettings.Format.IniFormat)
settings.remove("ingestion/source_root")
settings.setValue("library/enabled", False)
settings.sync()
del settings

import cockpit.ui.app as app_module
from cockpit.ui.config import ResolveOutcome

# The default data root on this PC also holds a database, which makes resolve_app_data_root refuse with a
# modal "multiple data roots" box even though COCKPIT_APP_DATA is set; the harness chooses the copy itself.
app_module.resolve_app_data_root = lambda: ResolveOutcome(chosen_root=work, probe_history=())
from cockpit.ui.main_window import MainWindow
from cockpit.layout.pdf_access import pdf_reading

pauses: list[dict] = []
started_at: dict[int, float] = {}
gui_ident = threading.get_ident()
results: dict = {}


def record(phase, info):
    ident = threading.get_ident()
    if phase == "start":
        started_at[ident] = time.perf_counter()
        return
    began = started_at.pop(ident, None)
    if began is None:
        return
    trigger = WINDOW[0]._collector.state.trigger if WINDOW else None
    pauses.append({"gui": ident == gui_ident, "gen": info.get("generation"),
                   "trigger": trigger.value if trigger else "explicit",
                   "ms": (time.perf_counter() - began) * 1000.0,
                   "busy": WINDOW[0].health_busy() if WINDOW else "none"})


WINDOW: list = []
steps: list = []


def schedule(delay_ms, action):
    steps.append((delay_ms, action))


def run_steps():
    if not steps:
        return
    delay, action = steps.pop(0)

    def fire():
        try:
            action()
        except Exception as error:
            results.setdefault("errors", []).append(f"{action.__name__}: {error!r}")
        run_steps()
    QTimer.singleShot(delay, fire)


def close_modals():
    widget = QApplication.activeModalWidget()
    if widget is not None:
        (widget.reject if hasattr(widget, "reject") else widget.close)()


def drive(window):
    WINDOW.append(window)
    gc.callbacks.append(record)
    closer = QTimer(window)
    closer.timeout.connect(close_modals)
    closer.start(300)
    conn = window._bootstrapped.conn
    rows = conn.execute(
        "SELECT a.id AS id, COUNT(c.id) AS n FROM active_audits a "
        "LEFT JOIN source_files s ON s.audit_id = a.id "
        "LEFT JOIN audit_bom_components c ON c.source_file_id = s.id "
        "GROUP BY a.id ORDER BY n DESC LIMIT 10"
    ).fetchall()
    largest = [row["id"] for row in rows]
    results["largest_audits_components"] = [row["n"] for row in rows]
    results["open_audits"] = len(window._audit_read_svc.list_open())
    results["paths_repointed"] = REPOINTED
    for round_index in range(5):
        for audit_id in largest:
            def open_audit(audit_id=audit_id):
                window._on_picker_audit_selected(audit_id)
            def back():
                window._reload_list()
            schedule(500, open_audit)
            schedule(3500, back)
    for _ in range(10):
        schedule(400, lambda: window._on_settings_requested())

    def start_ingest():
        from cockpit.ingestion.service import IngestionPlan, probe_identity
        paths = sorted(p for p in job.iterdir() if p.is_file())
        traveler = next(p for p in paths if "TRAVELER" in p.name.upper())
        service = window._bootstrapped.ingestion_service
        probed = probe_identity(traveler, service.coord_map)
        family = service.audit_repo.list_family(probed.part_number, probed.work_order_ref)
        plan = IngestionPlan(expected_identity=probed, replace_family=tuple(a.id for a in family), article_revision="FA")
        results["ingest_replaces"] = len(family)
        window._on_drop_received(paths, plan=plan)
    schedule(500, start_ingest)

    def wait_for_ingest():
        deadline = time.monotonic() + 240
        def poll():
            if window._busy.operation is None or time.monotonic() > deadline:
                results["ingest_finished"] = window._busy.operation is None
                schedule(65_000, finish)
                run_steps()
            else:
                QTimer.singleShot(500, poll)
        poll()
    steps.append((500, wait_for_ingest))

    def finish():
        gc.callbacks.remove(record)
        gui = [p for p in pauses if p["gui"]]
        by: dict = {}
        for p in gui:
            by.setdefault(f"gen{p['gen']}/{p['trigger']}", []).append(p["ms"])
        results["pauses"] = {
            key: {"n": len(v), "max_ms": round(max(v), 1), "p99_ms": round(sorted(v)[max(0, int(len(v) * 0.99) - 1)], 1),
                  "median_ms": round(statistics.median(v), 2)}
            for key, v in sorted(by.items())
        }
        results["ingest_young_max_ms"] = round(max([p["ms"] for p in gui if p["busy"] == "ingest" and p["gen"] in (0, 1)] or [0]), 1)
        results["off_thread_collections"] = sum(1 for p in pauses if not p["gui"])
        results["collector_off_thread"] = window._collector.state.off_thread_count
        results["gate_counts"] = list(window._bootstrapped.conn.gate_counts())
        reading = pdf_reading()
        results["pdf"] = {"sections": reading.sections, "max_hold_ms": round(reading.max_hold_ms), "max_wait_ms": round(reading.max_wait_ms)}
        summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        window.close()

    def finish_step():
        finish()
    run_steps()


original_show = MainWindow.show


def show_and_drive(self):
    original_show(self)

    def guarded():
        try:
            drive(self)
        except Exception as error:
            results.setdefault("errors", []).append(f"drive: {error!r}")
            summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
            self.close()
    QTimer.singleShot(3000, guarded)


MainWindow.show = show_and_drive
app_module.main()
