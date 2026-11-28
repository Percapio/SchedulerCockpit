"""Roadmap 02 M3: the restyle crash (F12). One process opens and unloads the largest audits of a data copy, then
restyles, alternating FontScaleController.request_delta(+1) and (-1) and processing events after each until the
restyle timer has fired. Run it under cdb as Roadmap 02 §4 M3 describes; the cdb log's FIRST and SECOND markers
count first-chance and fatal access violations.

Usage, from this repository's root, on the real display (accessibility must be active, as it is on this PC):
    python scripts/audit/m3_restyle_driver.py <code tree> <production v1 folder> <work root>
        [--audits 20] [--purges 0] [--restyles 20]

<code tree> is the repository cockpit is imported from: an export of 1.80 for the baseline, or this tree.
--purges N opens and unloads the 3 largest audits N more times each before the restyles (M3's escalation).
The work root is replaced by a fresh copy of the v1 folder (customer data: keep it outside the repo, delete it
afterwards). The process ends with os._exit(0) after printing "M3 DRIVER DONE", so neither tree's teardown
adds faults that aren't the restyle's.
"""
import argparse, os, pathlib, shutil, sqlite3, sys, time

parser = argparse.ArgumentParser()
parser.add_argument("tree", type=pathlib.Path)
parser.add_argument("source", type=pathlib.Path)
parser.add_argument("work", type=pathlib.Path)
parser.add_argument("--audits", type=int, default=20)
parser.add_argument("--purges", type=int, default=0)
parser.add_argument("--restyles", type=int, default=20)
args = parser.parse_args()
sys.path.insert(0, str(args.tree.resolve()))

work = args.work
if work.exists():
    shutil.rmtree(work)
shutil.copytree(args.source, work / "v1", ignore=shutil.ignore_patterns("logs", "crash_reports"))
(work / "v1" / "logs").mkdir()
db = sqlite3.connect(str(work / "v1" / "local_audit.db"))
sample = db.execute("SELECT local_storage_path FROM source_files WHERE local_storage_path LIKE '%uploads%' LIMIT 1").fetchone()
if sample:
    old = sample[0][: sample[0].lower().index("uploads") + len("uploads")]
    new = str(work / "v1" / "uploads")
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for column in [c[1] for c in db.execute(f"PRAGMA table_info({table})") if "path" in c[1].lower()]:
            db.execute(f"UPDATE {table} SET {column} = ? || substr({column}, ?) WHERE {column} LIKE ? || '%'",
                       (new, len(old) + 1, old))
    db.commit()
db.close()
os.environ["COCKPIT_APP_DATA"] = str(work)

from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QApplication

settings = QSettings(str(work / "v1" / "settings.ini"), QSettings.Format.IniFormat)
settings.remove("ingestion/source_root")
settings.setValue("library/enabled", False)
settings.sync()
del settings

import cockpit.ui.app as app_module
from cockpit.ui.config import ResolveOutcome
from cockpit.ui.main_window import MainWindow

print(f"M3 DRIVER app={pathlib.Path(app_module.__file__).resolve()}", flush=True)
app_module.resolve_app_data_root = lambda: ResolveOutcome(chosen_root=work, probe_history=())


def close_modals() -> None:
    widget = QApplication.activeModalWidget()
    if widget is not None:
        (widget.reject if hasattr(widget, "reject") else widget.close)()


def session(window):
    yield 2000
    rows = window._bootstrapped.conn.execute(
        "SELECT a.id AS id, COUNT(c.id) AS n FROM active_audits a "
        "LEFT JOIN source_files s ON s.audit_id = a.id "
        "LEFT JOIN audit_bom_components c ON c.source_file_id = s.id "
        "GROUP BY a.id ORDER BY n DESC, a.id LIMIT ?", (args.audits,)
    ).fetchall()
    largest = [row["id"] for row in rows]
    order = largest + [audit_id for audit_id in largest[:3] for _ in range(args.purges)]
    for audit_id in order:
        window._on_picker_audit_selected(audit_id)
        yield 3000
        window._reload_list()
        yield 500
    print(f"M3 DRIVER opened {len(order)}", flush=True)
    for index in range(args.restyles):
        window._font_scale.request_delta(1 if index % 2 == 0 else -1)
        yield 300
        deadline = time.monotonic() + 30
        while window._font_scale._restyle_timer.isActive() and time.monotonic() < deadline:
            yield 100
        QApplication.processEvents()
        yield 500
    print(f"M3 DRIVER restyled {args.restyles}", flush=True)
    print("M3 DRIVER DONE", flush=True)
    os._exit(0)


def drive(window, generator) -> None:
    def advance():
        try:
            delay = next(generator)
        except StopIteration:
            return
        QTimer.singleShot(delay, advance)
    advance()


original_show = MainWindow.show


def show_and_drive(self):
    original_show(self)
    closer = QTimer(self)
    closer.timeout.connect(close_modals)
    closer.start(300)
    QTimer.singleShot(3000, lambda: drive(self, session(self)))


MainWindow.show = show_and_drive
app_module.main()
