import pytest
from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import QApplication
from unittest.mock import MagicMock
import pathlib

from cockpit.services.second_ops import (
    SecondOpsSettingsController, SECOND_OPS_TERMS_KEY,
    SecondOpsCandidate, AuditCandidates, mount_label,
    SecondOpsRow
)
from cockpit.ui.widgets.second_ops_dialog import (
    SecondOpsOverviewDialog, SecondOpsAuditDialog, stored_dialog_size, DialogSize
)
from cockpit.ingestion.parsers.audit_bom import CANONICAL_COLUMNS, RawBomRow

@pytest.fixture
def terms_controller(monkeypatch, tmp_path):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    settings = QSettings()
    settings.remove(SECOND_OPS_TERMS_KEY)
    settings.sync()
    return SecondOpsSettingsController(settings)

def test_mount_label():
    assert mount_label('T') == 'THT'
    assert mount_label('S') == 'SMT'
    assert mount_label('X') == ''

def test_overview_columns(qtbot, terms_controller, tmp_path):
    repo = MagicMock()
    # Provide data
    line1 = MagicMock()
    line1.audit_id = 1
    line1.part_number = 'PN1'
    line1.split_suffix = ''
    line1.work_order_ref = 'WO1'
    line1.find_number = '10'
    line1.component_mpn = 'Fuse'
    line1.description = 'DESC1'
    line1.mount_type = 'T'
    repo.list_bom_lines_for_all_active_audits.return_value = [line1]
    
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    dialog = SecondOpsOverviewDialog(repo, terms_controller, settings)
    assert dialog.tree.columnCount() == 3
    assert dialog.tree.headerItem().text(0) == 'Line'
    assert dialog.tree.headerItem().text(1) == 'Mount'
    assert dialog.tree.headerItem().text(2) == 'Description'
    
    top = dialog.tree.topLevelItem(0)
    assert top.isFirstColumnSpanned()
    child = top.child(0)
    assert not child.isFirstColumnSpanned()
    
    assert child.text(0) == '10'
    assert child.text(1) == 'THT'
    assert child.text(2) == 'DESC1'
    
    # Column 0 width
    assert dialog.tree.columnWidth(0) > 10
    assert dialog.tree.columnWidth(0) != 100

def test_dialog_no_stored_size(qtbot, terms_controller, tmp_path):
    repo = MagicMock()
    line1 = MagicMock()
    line1.audit_id = 1
    line1.part_number = 'PN1'
    line1.split_suffix = ''
    line1.work_order_ref = 'WO1'
    line1.find_number = '10'
    line1.component_mpn = 'Fuse'
    line1.description = 'DESC1'
    line1.mount_type = 'T'
    repo.list_bom_lines_for_all_active_audits.return_value = [line1]
    
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    dialog = SecondOpsOverviewDialog(repo, terms_controller, settings)
    assert dialog.width() == 800
    assert dialog.height() == 600

def test_resize_then_close_then_reopen(qtbot, terms_controller, tmp_path):
    repo = MagicMock()
    line1 = MagicMock()
    line1.audit_id = 1
    line1.part_number = 'PN1'
    line1.split_suffix = ''
    line1.work_order_ref = 'WO1'
    line1.find_number = '10'
    line1.component_mpn = 'Fuse'
    line1.description = 'DESC1'
    line1.mount_type = 'T'
    repo.list_bom_lines_for_all_active_audits.return_value = [line1]
    
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    dialog = SecondOpsOverviewDialog(repo, terms_controller, settings)
    dialog.resize(900, 700)
    dialog.done(1)
    
    dialog2 = SecondOpsOverviewDialog(repo, terms_controller, settings)
    assert dialog2.width() == 900
    assert dialog2.height() == 700

def test_malformed_size_fallback(qtbot, terms_controller, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("second_ops/overview_size", "abc")
    
    repo = MagicMock()
    line1 = MagicMock()
    line1.audit_id = 1
    line1.part_number = 'PN1'
    line1.split_suffix = ''
    line1.work_order_ref = 'WO1'
    line1.find_number = '10'
    line1.component_mpn = 'Fuse'
    line1.description = 'DESC1'
    line1.mount_type = 'T'
    repo.list_bom_lines_for_all_active_audits.return_value = [line1]
    
    dialog = SecondOpsOverviewDialog(repo, terms_controller, settings)
    assert dialog.width() == 800
    assert dialog.height() == 600

    settings.setValue("second_ops/overview_size", "-1x-1")
    dialog2 = SecondOpsOverviewDialog(repo, terms_controller, settings)
    assert dialog2.width() == 800

def test_review_shared_key_last_close_wins(qtbot, terms_controller, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    repo = MagicMock()
    repo.list_for_audit.return_value = []
    
    dialogA = SecondOpsAuditDialog(1, repo, terms_controller, settings)
    dialogA.resize(850, 650)
    
    dialogB = SecondOpsAuditDialog(2, repo, terms_controller, settings)
    # Open B without resizing, wait, it gets 800x600 because we haven't closed A yet.
    assert dialogB.width() == 800
    
    dialogA.done(1)
    dialogB.done(1)
    
    # Last close wins, which was B (800x600)
    dialogC = SecondOpsAuditDialog(3, repo, terms_controller, settings)
    assert dialogC.width() == 800

def test_reject_calls_detach_worker(qtbot, terms_controller, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    repo = MagicMock()
    repo.list_for_audit.return_value = []
    dialog = SecondOpsAuditDialog(1, repo, terms_controller, settings)
    
    dialog._detach_worker = MagicMock()
    dialog.reject()
    
    # Overrides called in order: _detach_worker then done() (which we can check via settings write)
    dialog._detach_worker.assert_called_once()
    assert settings.value("second_ops/review_size") == f"{dialog.width()}x{dialog.height()}"
