import pytest
from PyQt6.QtCore import Qt, QSettings
import pathlib
import openpyxl
from PyQt6.QtWidgets import QApplication
from unittest.mock import MagicMock

from cockpit.services.second_ops import (
    SecondOpsSettingsController, SECOND_OPS_TERMS_KEY,
    RawBomRow, SecondOpsRow
)
from cockpit.ui.widgets.settings_dialog import SettingsDialog
from cockpit.ui.widgets.second_ops_dialog import SecondOpsAuditDialog, SecondOpsOverviewDialog
from cockpit.ingestion.parsers.audit_bom import CANONICAL_COLUMNS

@pytest.fixture
def terms_controller(monkeypatch, tmp_path):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    settings = QSettings()
    settings.remove(SECOND_OPS_TERMS_KEY)
    settings.sync()
    return SecondOpsSettingsController(settings)

# §7.2 Patch 01 Tests
def test_settings_dialog_escape_commits(qtbot, terms_controller):
    dialog = SettingsDialog(MagicMock(), MagicMock(), None, terms_controller, MagicMock())
    dialog.terms_edit.setText("New Term")
    # Simulate Escape key which calls reject()
    dialog.reject()
    assert "New Term" in terms_controller.terms()

def test_settings_dialog_close_commits(qtbot, terms_controller):
    dialog = SettingsDialog(MagicMock(), MagicMock(), None, terms_controller, MagicMock())
    dialog.terms_edit.setText("Another Term")
    dialog.show()
    dialog.close()
    assert "Another Term" in terms_controller.terms()

def test_settings_dialog_destruction(qtbot, terms_controller):
    dialog = SettingsDialog(MagicMock(), MagicMock(), None, terms_controller, MagicMock())
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

# §7.3 Patch 09b Tests
from cockpit.services.second_ops import render_tsv
from cockpit.ingestion.parsers.audit_bom import SHEET_COLUMN_ORDER, REQUIRED_HEADER, OPTIONAL_HEADER
from cockpit.services.second_ops import SecondOpsRow

def test_sheet_column_order_against_constants():
    without_optionals = [label for label in SHEET_COLUMN_ORDER if label not in OPTIONAL_HEADER]
    assert without_optionals == REQUIRED_HEADER
    assert set(SHEET_COLUMN_ORDER) == set(CANONICAL_COLUMNS)
    assert len(set(SHEET_COLUMN_ORDER)) == 14

def test_render_tsv_distinct_values():
    cells = tuple([f"Val_{col}" for col in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    tsv = render_tsv([row])
    fields = tsv.split("\t")
    
    # 0,1,2,11,12,13,3,4,5,6,7,8,9,10 permutation
    assert fields[0] == "Val_Find#"
    assert fields[1] == "Val_PartNum"
    assert fields[2] == "Val_Count"
    assert fields[3] == "Val_MSL level"
    assert fields[4] == "Val_Date code"
    assert fields[5] == "Val_Baked date"
    assert fields[6] == "Val_Ref_Des"
    assert fields[7] == "Val_Package"
    assert fields[8] == "Val_Description"
    assert fields[9] == "Val_SMT/THT"
    assert fields[10] == "Val_Qty Need"
    assert fields[11] == "Val_Qty On hand"
    assert fields[12] == "Val_Qty short"
    assert fields[13] == "Val_comment"

def test_render_tsv_retained_indices():
    cells = tuple([col for col in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    fields = render_tsv([row]).split("\t")
    
    assert fields[0] == "Find#"
    assert fields[1] == "PartNum"
    assert fields[2] == "Count"
    assert fields[6] == "Ref_Des"
    assert fields[8] == "Description"
    assert fields[9] == "SMT/THT"
    assert fields[10] == "Qty Need"
    assert fields[11] == "Qty On hand"

def test_render_tsv_gather_not_scatter():
    cells = tuple([col for col in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    fields = render_tsv([row]).split("\t")
    assert fields[6] == "Ref_Des"
    assert fields[6] != "SMT/THT"

def test_legacy_workbook_optionals():
    cells = tuple(["Val"] * 11 + ["", "", ""])
    row = RawBomRow(1, cells, "", "")
    fields = render_tsv([row]).split("\t")
    assert len(fields) == 14
    assert fields[3] == ""
    assert fields[4] == ""
    assert fields[5] == ""
    assert fields[6] == "Val"

def test_row_only_discarded_column():
    cells = tuple(["" if c != "Package" else "PackageVal" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    fields = render_tsv([row]).split("\t")
    
    assert len(fields) == 14
    for idx in [0, 1, 2, 6, 8, 9, 10, 11]:
        assert fields[idx] == ""
    assert fields[7] == "PackageVal"

def test_empty_ref_des_no_filler():
    cells = tuple(["Val" if c != "Ref_Des" else "" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    fields = render_tsv([row]).split("\t")
    assert fields[6] == ""

def test_adjacent_empty_cells():
    cells = tuple(["" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    payload = render_tsv([row])
    fields = payload.split("\t")
    assert len(fields) == 14
    assert "\t\t" in payload

def test_render_embedded_newline():
    cells = tuple(["Line1\nLine2" if c == "Find#" else "Val" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    payload = render_tsv([row])
    fields = payload.split("\t")
    assert fields[0] == "Line1 Line2"
    assert "\n" not in fields[0]

def test_render_tab():
    cells = tuple(["Val\tVal" if c == "Find#" else "Val" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    payload = render_tsv([row])
    fields = payload.split("\t")
    assert len(fields) == 14
    assert fields[0] == "Val Val"

def test_unescaped_markup():
    cells = tuple(["<script>&" if c == "Find#" else "Val" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    fields = render_tsv([row]).split("\t")
    assert fields[0] == "<script>&"

def test_multi_row_copy():
    cells = tuple(["Val" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    payload = render_tsv([row, row])
    lines = payload.split("\r\n")
    assert len(lines) == 2
    assert not lines[1].endswith("\t")

class _RecordingClipboard:
    """Stands in for the system clipboard.

    Qt cannot take the Windows clipboard while another process holds it, and on
    a headless runner there is none at all: a bare setText/text round trip
    outside Cockpit comes back empty. Reading the payload back through the OS
    therefore tests the environment rather than the dialog, so these two assert
    on what the dialog hands over. Every other test in this file already builds
    its payload with render_tsv for the same reason.
    """

    def __init__(self):
        self.text_payload = None
        self.mime_payloads = []

    def clear(self):
        self.text_payload = None
        self.mime_payloads.clear()

    def setText(self, text):
        self.text_payload = text

    def setMimeData(self, mime):
        self.mime_payloads.append(mime)


@pytest.fixture
def recording_clipboard(monkeypatch):
    clipboard = _RecordingClipboard()
    monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: clipboard))
    return clipboard


def test_hidden_columns_still_copied(qtbot, terms_controller, recording_clipboard):
    """Hiding a column in the view must not drop it from the payload: the copy
    is built from the row, not from whatever happens to be on screen."""
    repo = MagicMock()
    repo.list_for_audit.return_value = []
    dialog = SecondOpsAuditDialog(1, repo, terms_controller)
    cells = tuple(["Val" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    dialog._on_rows_ready([SecondOpsRow(row, True)])

    dialog.table.setColumnHidden(2, True)
    dialog._copy_to_clipboard([row])

    fields = recording_clipboard.text_payload.split("\t")
    assert len(fields) == 14
    assert fields[2] == "Val"

def test_clipboard_payload_mime_types(qtbot, terms_controller, recording_clipboard):
    """Plain text only. An HTML flavour would paste markup into Excel."""
    from cockpit.ui.widgets.second_ops_dialog import SecondOpsAuditDialog
    repo = MagicMock()
    repo.list_for_audit.return_value = []
    dialog = SecondOpsAuditDialog(1, repo, terms_controller)
    cells = tuple(["Val" for c in CANONICAL_COLUMNS])
    row = RawBomRow(1, cells, "", "")
    dialog._on_rows_ready([SecondOpsRow(row, True)])

    dialog._copy_to_clipboard([row])

    assert recording_clipboard.text_payload is not None
    assert recording_clipboard.mime_payloads == []

# §7.4 Feature 01 Tests
def test_second_ops_audit_dialog_is_modeless(qtbot, terms_controller):
    repo = MagicMock()
    repo.list_for_audit.return_value = []
    dialog = SecondOpsAuditDialog(1, repo, terms_controller)
    assert not dialog.isModal()

def test_second_ops_overview_dialog_is_modal(qtbot, terms_controller):
    dialog = SecondOpsOverviewDialog(MagicMock(), terms_controller)
    assert dialog.isModal()
