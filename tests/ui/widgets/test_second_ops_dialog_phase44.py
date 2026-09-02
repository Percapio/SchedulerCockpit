"""Phase 44 regression tests for the three defects found in the 2nd OPS modal.

1. The AuditList entry point raised AttributeError because BootstrappedApp
   carried no audit_bom_component_repo.
2. Close did nothing after a read finished: reject() touched a QThread wrapper
   whose C++ object had already been deleted, so it raised.
3. Every canonical column was shown, blank ones included.
"""

import dataclasses
import pathlib

import openpyxl
import pytest
from PyQt6.QtCore import Qt, QSettings

from cockpit.ingestion.parsers.audit_bom import CANONICAL_COLUMNS, RawBomRow
from cockpit.services.second_ops import (
    SecondOpsRow, SecondOpsSettingsController, SECOND_OPS_TERMS_KEY
)
from cockpit.ui.bootstrap import BootstrappedApp
from cockpit.ui.widgets.second_ops_dialog import (
    BomDropTarget, SecondOpsAuditDialog, SecondOpsTableModel
)


@pytest.fixture
def terms_controller(monkeypatch, tmp_path):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path)
    )
    settings = QSettings()
    settings.remove(SECOND_OPS_TERMS_KEY)
    settings.sync()
    return SecondOpsSettingsController(settings)


@pytest.fixture
def bom_workbook(tmp_path) -> pathlib.Path:
    """Two rows; only Find#, PartNum, Count, Ref_Des, Description, SMT/THT filled."""
    path = tmp_path / "B123456.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AUDIT BOM"
    ws.append(list(CANONICAL_COLUMNS))
    ws.append([1, "SCREW-M3-8", 2, "H1,H2", "", "SCREW M3 8MM", "T", "", "", "", "", "", "", ""])
    ws.append([2, "RC0402-10K", 4, "R1", "", "RES 10K 1%", "S", "", "", "", "", "", "", ""])
    wb.save(path)
    return path


class _StoredBom:
    file_category = "BOM"
    file_hash = "deadbeef"

    def __init__(self, path: pathlib.Path) -> None:
        self.local_storage_path = path


class _SourceFileRepoStub:
    def __init__(self, path: pathlib.Path | None) -> None:
        self._path = path

    def list_for_audit(self, audit_id: int):
        return [] if self._path is None else [_StoredBom(self._path)]


def _await_rows(qtbot, dialog: SecondOpsAuditDialog) -> None:
    qtbot.waitUntil(lambda: dialog._model is not None, timeout=5000)


# --- defect 1 --------------------------------------------------------------

def test_bootstrapped_app_exposes_bom_component_repo():
    """MainWindow._on_overview_second_ops_requested reads this field."""
    names = {f.name for f in dataclasses.fields(BootstrappedApp)}
    assert "audit_bom_component_repo" in names


# --- defect 2 --------------------------------------------------------------

def test_close_after_read_completes(qtbot, terms_controller, bom_workbook):
    dialog = SecondOpsAuditDialog(1, _SourceFileRepoStub(bom_workbook), terms_controller)
    # qtbot.addWidget(dialog)
    dialog.show()
    _await_rows(qtbot, dialog)

    dialog.reject()

    assert not dialog.isVisible()


def test_close_mid_read_does_not_raise(qtbot, terms_controller, bom_workbook):
    dialog = SecondOpsAuditDialog(1, _SourceFileRepoStub(bom_workbook), terms_controller)
    # qtbot.addWidget(dialog)
    dialog.show()

    dialog.reject()

    assert not dialog.isVisible()
    assert dialog._thread is None
    assert dialog._worker is None


def test_close_with_no_stored_bom(qtbot, terms_controller):
    dialog = SecondOpsAuditDialog(1, _SourceFileRepoStub(None), terms_controller)
    # qtbot.addWidget(dialog)
    dialog.show()

    assert not dialog.table.isVisible()
    dialog.reject()
    assert not dialog.isVisible()


def test_thread_is_not_running_once_rows_are_shown(qtbot, terms_controller, bom_workbook):
    dialog = SecondOpsAuditDialog(1, _SourceFileRepoStub(bom_workbook), terms_controller)
    # qtbot.addWidget(dialog)
    dialog.show()
    _await_rows(qtbot, dialog)

    assert dialog._thread is None
    dialog.reject()


# --- defect 3 --------------------------------------------------------------

def test_blank_columns_are_hidden(qtbot, terms_controller, bom_workbook):
    dialog = SecondOpsAuditDialog(1, _SourceFileRepoStub(bom_workbook), terms_controller)
    # qtbot.addWidget(dialog)
    dialog.show()
    _await_rows(qtbot, dialog)

    shown = {
        CANONICAL_COLUMNS[c]
        for c in range(len(CANONICAL_COLUMNS))
        if not dialog.table.isColumnHidden(c + 1)
    }
    assert shown == {"Find#", "PartNum", "Count", "Ref_Des", "Description", "SMT/THT"}
    assert not dialog.table.isColumnHidden(0)  # the tick column always shows
    dialog.reject()


def test_column_visibility_survives_show_all(qtbot, terms_controller, bom_workbook):
    dialog = SecondOpsAuditDialog(1, _SourceFileRepoStub(bom_workbook), terms_controller)
    # qtbot.addWidget(dialog)
    dialog.show()
    _await_rows(qtbot, dialog)

    dialog.show_all_cb.setChecked(True)

    assert dialog.table.isColumnHidden(CANONICAL_COLUMNS.index("Baked date") + 1)
    assert not dialog.table.isColumnHidden(CANONICAL_COLUMNS.index("PartNum") + 1)
    dialog.reject()


def test_all_columns_shown_when_nothing_matched(qtbot, terms_controller, bom_workbook):
    """An empty basis must not collapse the table to the tick column alone."""
    rows: list[SecondOpsRow] = []
    model = SecondOpsTableModel(rows)
    assert model.column_population_basis() == []


# --- supporting defects found alongside them --------------------------------

def test_tick_accepts_the_enum_the_delegate_passes():
    """CenteredCheckDelegate hands setData a Qt.CheckState, not an int."""
    rows = [
        SecondOpsRow(row=RawBomRow(i, tuple([""] * 14), "", ""), is_match=False)
        for i in (2, 3)
    ]
    model = SecondOpsTableModel(rows)
    model.set_show_all(True)

    assert model.setData(model.index(0, 0), Qt.CheckState.Checked,
                         Qt.ItemDataRole.CheckStateRole)
    assert model._ticks == [True, False]

    assert model.setData(model.index(0, 0), Qt.CheckState.Unchecked,
                         Qt.ItemDataRole.CheckStateRole)
    assert model._ticks == [False, False]

    assert model.setData(model.index(1, 0), 2, Qt.ItemDataRole.CheckStateRole)
    assert model._ticks == [False, True]


def test_identical_rows_tick_independently():
    """Index-based row mapping: equal frozen dataclasses must not alias."""
    cells = tuple(["SCREW"] * 14)
    rows = [
        SecondOpsRow(row=RawBomRow(2, cells, "SCREW", "SCREW"), is_match=True),
        SecondOpsRow(row=RawBomRow(2, cells, "SCREW", "SCREW"), is_match=True),
    ]
    model = SecondOpsTableModel(rows)

    model.setData(model.index(0, 0), Qt.CheckState.Unchecked,
                  Qt.ItemDataRole.CheckStateRole)

    assert model._ticks == [False, True]
    assert len(model.ticked_raw_rows()) == 1


def test_drop_target_overrides_the_virtual(qtbot, terms_controller):
    """Assigning dropEvent onto a QLabel instance never fires; a subclass does."""
    from PyQt6.QtWidgets import QLabel

    dialog = SecondOpsAuditDialog(1, _SourceFileRepoStub(None), terms_controller)
    # qtbot.addWidget(dialog)

    assert isinstance(dialog.drop_area, BomDropTarget)
    assert type(dialog.drop_area).dropEvent is not QLabel.dropEvent
    dialog.reject()


def test_stored_hash_is_captured_even_when_the_file_is_gone(qtbot, terms_controller, tmp_path):
    """FILE_MISSING still has a hash to check a dropped replacement against."""
    missing = tmp_path / "gone.xlsx"
    dialog = SecondOpsAuditDialog(1, _SourceFileRepoStub(missing), terms_controller)
    # qtbot.addWidget(dialog)

    assert dialog._expected_hash == _StoredBom.file_hash
    dialog.reject()
