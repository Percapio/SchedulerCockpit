from collections import Counter
import hashlib
import json
import pathlib

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from cockpit.ingestion.errors import MalformedBomError
from cockpit.ingestion.parsers.audit_bom import parse, summarise_cleaning
from cockpit.ingestion.roles import SourceRole, role_of
from cockpit.ui.bootstrap import bootstrap
from cockpit.ui.config import resolve_config
from cockpit.ui.main_window import MainWindow
from cockpit.ui.theme import ThemeLoader
from cockpit.ui.widgets.cleaning_dialogs import CleaningChangesDialog
from cockpit.ui.workers.ingestion_worker import AuditSummary


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DATA_ROOT = REPO_ROOT / "backend" / "data"
CORPUS_DIR = DATA_ROOT / "data"
EXPECTATIONS_PATH = DATA_ROOT / "phase53_expectations.json"


def _items_digest(items_tuples: list) -> str:
    payload = json.dumps(items_tuples, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.skipif(
    not CORPUS_DIR.is_dir() or not EXPECTATIONS_PATH.is_file(),
    reason="Local corpus or expectations file is absent",
)
def test_local_corpus_regression():
    expectations = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))

    all_boms = sorted(
        p for p in CORPUS_DIR.rglob("*.xlsx")
        if p.is_file() and "audit bom" in p.name.lower()
    )
    lock_files = [p for p in all_boms if p.name.startswith("~$")]
    real_files = [p for p in all_boms if not p.name.startswith("~$")]

    assert len(lock_files) == 20
    assert len(real_files) == 1721
    for lf in lock_files:
        assert role_of(lf.name) == SourceRole.UNKNOWN

    parsed_count = 0
    failures: dict[str, str] = {}
    changed_from_old: list[str] = []
    old_item_digests: dict[str, str] = expectations["old_item_digests"]
    expected_changed_digests: dict[str, str] = expectations["new_item_digests_for_changed"]

    for p in real_files:
        rel = p.relative_to(CORPUS_DIR).as_posix()
        try:
            res = parse(p)
            parsed_count += 1
            if rel in old_item_digests:
                items_list = [
                    [it.find_number, it.component_mpn, list(it.ref_des_list), it.mount_type]
                    for it in res.items
                ]
                dig = _items_digest(items_list)
                if dig != old_item_digests[rel]:
                    changed_from_old.append(rel)
                    assert dig == expected_changed_digests.get(rel), (
                        f"Unexpected item change in {rel}"
                    )
        except MalformedBomError as exc:
            failures[rel] = exc.reason

    assert parsed_count >= 1696
    reason_counts = dict(Counter(failures.values()))
    assert reason_counts == {
        "DUPLICATE_MPN": 15,
        "NO_HEADERED_SHEET": 6,
        "MISSING_COLUMN": 1,
        "INVALID_REFDES_TOKEN": 2,
        "INVALID_FIND_NUMBER": 1,
    }
    assert sorted(changed_from_old) == sorted(expectations["changed_files"])
    assert len(changed_from_old) == 6


@pytest.mark.skipif(
    not EXPECTATIONS_PATH.is_file(),
    reason="Local expectations file is absent",
)
def test_local_acceptance_standard_ingest(qtbot, tmp_path, monkeypatch):
    expectations = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    job_dir_name = expectations.get("acceptance_job_dir")
    if not job_dir_name or not (DATA_ROOT / job_dir_name).is_dir():
        pytest.skip("Local acceptance job directory is absent")

    job_dir = DATA_ROOT / job_dir_name
    paths = sorted(p for p in job_dir.iterdir() if p.is_file() and not p.name.startswith("~$"))
    bom_paths = [p for p in paths if role_of(p.name) == SourceRole.BOM]
    assert len(bom_paths) == 1

    # 1. Verify BOM parse details directly
    bom_res = parse(bom_paths[0])
    assert len(bom_res.items) == 7
    smt_items = [it for it in bom_res.items if it.mount_type == "S"]
    tht_items = [it for it in bom_res.items if it.mount_type == "T"]
    assert len(smt_items) == 6
    assert len(tht_items) == 1
    assert bom_res.excluded_dni_count == 1

    cleaning_summary = summarise_cleaning(bom_res.cleaning)
    assert cleaning_summary.consequential == []
    assert cleaning_summary.routine_counts == {}

    # 2. Verify full Standard ingest and UI presentation
    root = tmp_path / "cockpit_data"
    root.mkdir()
    monkeypatch.setenv("COCKPIT_APP_DATA", str(root))
    config = resolve_config(root / "v1")
    bootstrapped = bootstrap(config)

    ui_dir = REPO_ROOT / "cockpit" / "ui"
    theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(
        QApplication.instance(),
        bootstrapped,
        bootstrapped.audit_read_svc,
        bootstrapped.checklist_svc,
        bootstrapped.split_svc,
        bootstrapped.completion_svc,
        bootstrapped.layout_query_svc,
        bootstrapped.pdf_renderer,
        bootstrapped.holiday_svc,
        theme=theme,
        settings=settings,
    )
    qtbot.addWidget(window)

    try:
        changes_dialogs_opened = []
        import cockpit.ui.widgets.cleaning_dialogs as cd_mod

        class SpyChangesDialog(CleaningChangesDialog):
            def exec(self):
                changes_dialogs_opened.append(True)
                return super().accept()

        monkeypatch.setattr(cd_mod, "CleaningChangesDialog", SpyChangesDialog)

        last_detail = {}

        def _on_progress(ev):
            if ev.detail:
                last_detail.update(ev.detail)

        audit = bootstrapped.ingestion_service.ingest(paths, progress=_on_progress)
        summary = AuditSummary(
            audit_id=audit.id,
            part_number=audit.part_number,
            work_order_ref=audit.work_order_ref,
            tht_item_count=last_detail["tht_item_count"],
            eco_item_count=last_detail["eco_item_count"],
            cleaning=last_detail["cleaning"],
        )
        assert summary.tht_item_count == 1
        assert summary.cleaning.consequential == []

        window._on_ingest_succeeded(summary)

        assert changes_dialogs_opened == []
        toast_sub = window.toast.subtitle_label.text()
        assert "1 THT items" in toast_sub
        assert "cleaned" not in toast_sub
        assert "change to review" not in toast_sub

        # Verify persisted THT checklist has 1 item
        view = window._audit_view._session.current_view()
        assert view is not None
        assert len(view.tht_rows) == 1
    finally:
        window.shutdown_render_thread(timeout_ms=2000)
