"""Phase 51 sections 3.4, 4.3 and 6 — menu, column and confirmation."""

from datetime import datetime, timezone

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGroupBox, QMenu

from cockpit.ingestion.locator import IngestionScope
from cockpit.persistence.types import AuditStatus
from cockpit.services.date_urgency import DateUrgency
from cockpit.services.ingestion_mode import IngestionMode
from cockpit.services.replacement import ReplacementCasualty, summarise_family
from cockpit.services.repeat import derive_repeat_marker
from cockpit.services.views import OpenAuditDigest
from cockpit.ui.widgets.open_audit_picker import (
    FIRST_ARTICLE_TEXT, Column, OpenAuditPicker, RowKind,
)
from cockpit.ui.widgets.replace_confirm_dialog import ReplaceConfirmDialog


def digest(article_revision=None):
    return OpenAuditDigest(
        audit_id=1, part_number="B123456", work_order_ref="WO", split_suffix="",
        quantity=10, status=AuditStatus.NOT_CLEAR,
        updated_at=datetime.now(timezone.utc), date_ingested=datetime.now(timezone.utc),
        ship_date=None, lead_time_days=None,
        repeat=derive_repeat_marker({"assembly_type": "NEW"}),
        classification="Non-ITAR", assembly_class=2, process="LEAD FREE",
        feeder_setuptime=1.0, smt_runtime=2.0, tht_runtime=3.0, aoi_runtime=4.0,
        ops_runtime=5.0, shipping_runtime=6.0, start_by=None,
        article_revision=article_revision,
        start_by_urgency=DateUrgency.COMFORTABLE, ship_urgency=DateUrgency.COMFORTABLE,
    )


def data_index(picker, column):
    for row in range(picker.model.rowCount()):
        idx = picker.model.index(row, column)
        payload = idx.data(Qt.ItemDataRole.UserRole)
        if payload and payload["kind"] == RowKind.DATA:
            return idx
    raise AssertionError("no data row")


# --- section 4.3, the column ------------------------------------------------

def test_article_is_column_twenty():
    assert Column.PHOTOS == 19
    assert Column.ARTICLE_REVISION == 20


def test_column_count_and_header(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    assert len(picker.model.COLUMNS) == 21
    assert picker.model.COLUMNS[Column.ARTICLE_REVISION] == "Article"


@pytest.mark.parametrize(
    "stored,rendered", [(None, FIRST_ARTICLE_TEXT), ("FA", "FA"), ("2nd", "2nd")]
)
def test_null_reads_as_first_article(qtbot, stored, rendered):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([digest(article_revision=stored)])

    assert data_index(picker, Column.ARTICLE_REVISION).data(
        Qt.ItemDataRole.DisplayRole) == rendered


def test_article_is_searchable(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    assert "2nd" in picker._searchable_text(digest("2nd"))
    assert "fa" in picker._searchable_text(digest(None))


# --- section 6, the menu ----------------------------------------------------

def submenu(picker, label):
    for action in picker._new_menu.actions():
        if action.text() == label:
            return action.menu()
    return None


def test_three_modes_with_article_revision_fetch_only(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)

    labels = [a.text() for a in picker._new_menu.actions()]
    assert labels == ["Standard", "Split Job", "Article Revision..."]

    for mode_label in ("Standard", "Split Job"):
        entries = [a.text() for a in submenu(picker, mode_label).actions()]
        assert entries == ["Fetch by job number...", "Manual upload..."]

    # Article Revision is a leaf: its ordinal comes from a folder on the share,
    # and a manual drop carries nothing to read it from.
    assert submenu(picker, "Article Revision...") is None


def test_each_entry_emits_its_mode(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)

    with qtbot.waitSignal(picker.fetch_job_requested) as blocker:
        submenu(picker, "Split Job").actions()[0].trigger()
    assert blocker.args == [IngestionMode.SPLIT]

    with qtbot.waitSignal(picker.new_audit_requested) as blocker:
        submenu(picker, "Standard").actions()[1].trigger()
    assert blocker.args == [IngestionMode.STANDARD]

    with qtbot.waitSignal(picker.fetch_job_requested) as blocker:
        next(a for a in picker._new_menu.actions()
             if a.text() == "Article Revision...").trigger()
    assert blocker.args == [IngestionMode.ARTICLE_REVISION]


def test_mode_implications():
    assert IngestionMode.STANDARD.scope == IngestionScope.ROOT
    assert IngestionMode.SPLIT.scope == IngestionScope.ROOT
    assert IngestionMode.ARTICLE_REVISION.scope == IngestionScope.ARTICLE_SUBFOLDER
    assert IngestionMode.ARTICLE_REVISION.prompts_for_quantity is True
    assert IngestionMode.STANDARD.prompts_for_quantity is False
    assert IngestionMode.SPLIT.splits_after_ingest is True
    assert IngestionMode.ARTICLE_REVISION.splits_after_ingest is False


# --- section 3.4, the confirmation ------------------------------------------

def casualty(suffix="", article="FA", **kw):
    base = dict(
        split_suffix=suffix, quantity=10, status=AuditStatus.SMT,
        article_revision=article, ops_per_board_min=2.5,
        is_labeled=True, are_photos_uploaded=False, tht_item_count=7,
    )
    base.update(kw)
    return ReplacementCasualty(**base)


def test_the_dialog_names_both_articles(qtbot):
    dialog = ReplaceConfirmDialog("B123456", "WO-1", "2nd", [casualty(article="FA")])
    qtbot.addWidget(dialog)

    from PyQt6.QtWidgets import QLabel
    text = " ".join(w.text() for w in dialog.findChildren(QLabel))
    assert "B123456" in text and "WO-1" in text
    assert "<b>FA</b>" in text
    assert "<b>2nd</b>" in text


def test_the_dialog_lists_one_entry_per_family_member(qtbot):
    casualties = [casualty("-1"), casualty("-2", quantity=40)]
    dialog = ReplaceConfirmDialog("B123456", "WO-1", "FA", casualties)
    qtbot.addWidget(dialog)

    from PyQt6.QtWidgets import QLabel
    text = " ".join(w.text() for w in dialog.findChildren(QLabel))
    assert "-1" in text and "-2" in text
    assert "all 2 of its rows" in text


def test_the_dialog_enumerates_what_is_lost(qtbot):
    dialog = ReplaceConfirmDialog("B123456", "WO-1", "FA", [casualty()])
    qtbot.addWidget(dialog)

    from PyQt6.QtWidgets import QLabel
    text = " ".join(w.text() for w in dialog.findChildren(QLabel))
    assert "Quantity 10" in text
    assert "OPS per board 2.5" in text
    assert "Labelled" in text
    assert "7 THT checklist items" in text
    assert "cannot be undone" in text


def test_cancel_is_the_default_button(qtbot):
    """An irreversible delete should not be one stray Return away."""
    dialog = ReplaceConfirmDialog("B123456", "WO-1", "FA", [casualty()])
    qtbot.addWidget(dialog)
    assert dialog.cancel_btn.isDefault() is True
    assert dialog.replace_btn.isDefault() is False


def test_an_unsplit_row_is_labelled_as_such(qtbot):
    assert casualty("").label == "(unsplit)"
    assert casualty("-1").label == "-1"


def test_summarise_family_reports_each_member():
    from types import SimpleNamespace

    family = [
        SimpleNamespace(id=1, split_suffix="-1", quantity=6, status=AuditStatus.SMT,
                        article_revision="2nd", ops_per_board_min=1.5,
                        is_labeled=True, are_photos_uploaded=False),
        SimpleNamespace(id=2, split_suffix="-2", quantity=4, status=AuditStatus.FSU,
                        article_revision=None, ops_per_board_min=None,
                        is_labeled=False, are_photos_uploaded=True),
    ]
    repo = SimpleNamespace(list_for_audit=lambda aid: [object()] * aid)

    casualties = summarise_family(family, repo)

    assert [c.split_suffix for c in casualties] == ["-1", "-2"]
    assert casualties[0].tht_item_count == 1
    assert casualties[1].tht_item_count == 2
    assert casualties[1].article_revision == "FA", "NULL reads as first article"
