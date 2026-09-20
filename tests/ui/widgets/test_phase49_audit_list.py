"""Phase 49 section 8.3 — Audit List columns, Wash, and the repeat cue."""

from datetime import datetime, timezone

import pytest
from PyQt6.QtCore import QSettings, Qt

from cockpit.persistence.types import AuditStatus
from cockpit.services.date_urgency import DateUrgency
from cockpit.services.repeat import derive_repeat_marker
from cockpit.services.views import OpenAuditDigest
from cockpit.ui import facelift
from cockpit.persistence.traveler_flags import WashState
from cockpit.ui.widgets.open_audit_picker import (
    WASH_COLUMN_TEXT,
    Column,
    OpenAuditPicker,
    RepeatReferenceDelegate,
    RowKind,
    _CHECKBOX_COLUMNS,
    _DEFAULT_COLUMN_WIDTHS,
    _STATUS_STAGE_COLUMN,
)


def create_digest(
    audit_id=1,
    status=AuditStatus.NOT_CLEAR,
    assembly_class=2,
    process_clean=None,
    repeat_meta=None,
):
    return OpenAuditDigest(
        audit_id=audit_id,
        part_number="PN-49",
        work_order_ref="WO-49",
        split_suffix="",
        quantity=10,
        status=status,
        updated_at=datetime.now(timezone.utc),
        date_ingested=datetime.now(timezone.utc),
        ship_date=None,
        lead_time_days=None,
        repeat=derive_repeat_marker(repeat_meta or {"assembly_type": "NEW"}),
        classification="Non-ITAR",
        assembly_class=assembly_class,
        process="LEAD FREE",
        feeder_setuptime=1.0,
        smt_runtime=2.0,
        tht_runtime=3.0,
        aoi_runtime=4.0,
        ops_runtime=5.0,
        shipping_runtime=6.0,
        start_by=None,
        process_clean=process_clean,
        start_by_urgency=DateUrgency.COMFORTABLE,
        ship_urgency=DateUrgency.COMFORTABLE,
    )


def data_row_index(picker, column):
    for row in range(picker.model.rowCount()):
        idx = picker.model.index(row, column)
        payload = idx.data(Qt.ItemDataRole.UserRole)
        if payload and payload["kind"] == RowKind.DATA:
            return idx
    raise AssertionError("no data row")


# --- column ladder ----------------------------------------------------------

def test_wash_sits_right_of_process_and_photos_moved_to_the_end():
    assert Column.PROCESS == 9
    assert Column.WASH == 10
    assert Column.FSU == 11
    assert Column.PHOTOS == 19


def test_column_count_and_header_label(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    assert len(picker.model.COLUMNS) == 20
    assert picker.model.COLUMNS[Column.WASH] == "Wash"


def test_every_column_has_a_default_width():
    assert set(_DEFAULT_COLUMN_WIDTHS) == set(Column)


def test_status_stage_highlight_follows_the_enum(qtbot):
    """The FSU stage cue must land on the shifted FSU column, not the old index."""
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([create_digest(status=AuditStatus.FSU)])

    assert _STATUS_STAGE_COLUMN[AuditStatus.FSU] == Column.FSU == 11
    idx = data_row_index(picker, Column.FSU)
    assert idx.data(Qt.ItemDataRole.ForegroundRole) == facelift.attention_color()


# --- Wash -------------------------------------------------------------------

@pytest.mark.parametrize(
    "process_clean,expected",
    [
        ("CLEAN", "C"),
        ("WASH", "C"),
        ("NO CLEAN", "NC"),
        ("NO-CLEAN", "NC"),
        # A traveler ingested before process_clean was mapped said nothing.
        (None, ""),
        ("   ", ""),
    ],
)
def test_wash_renders_the_travelers_own_wording(qtbot, process_clean, expected):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([create_digest(process_clean=process_clean)])

    idx = data_row_index(picker, Column.WASH)
    assert idx.data(Qt.ItemDataRole.DisplayRole) == expected


def test_spelled_out_no_clean_is_not_read_as_a_wash_process(qtbot):
    """The inversion the is_clean_process boolean could not avoid."""
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([create_digest(process_clean="NO CLEAN")])

    idx = data_row_index(picker, Column.WASH)
    assert idx.data(Qt.ItemDataRole.DisplayRole) == WASH_COLUMN_TEXT[WashState.NO_CLEAN]


def test_wash_is_inert(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([create_digest(process_clean="CLEAN")])

    idx = data_row_index(picker, Column.WASH)
    assert Column.WASH not in _CHECKBOX_COLUMNS
    assert idx.data(Qt.ItemDataRole.CheckStateRole) is None
    assert not (picker.model.flags(idx) & Qt.ItemFlag.ItemIsUserCheckable)


def test_wash_search_tokens_isolate_each_state(qtbot):
    """"c" and "nc" would collide; "clean" is a substring of "noclean"."""
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)

    clean = picker._searchable_text(create_digest(process_clean="CLEAN"))
    dirty = picker._searchable_text(create_digest(process_clean="NO CLEAN"))
    silent = picker._searchable_text(create_digest(process_clean=None))

    assert "wash" in clean and "noclean" not in clean
    assert "noclean" in dirty and "wash" not in dirty
    assert "wash" not in silent and "noclean" not in silent


# --- Class 3 ----------------------------------------------------------------

def test_class_three_is_red_and_class_two_is_not(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)

    picker.populate([create_digest(assembly_class=3)])
    idx = data_row_index(picker, Column.ASSEMBLY_CLASS)
    assert idx.data(Qt.ItemDataRole.DisplayRole) == "3"
    assert idx.data(Qt.ItemDataRole.ForegroundRole) == facelift.overdue_color()

    picker.populate([create_digest(assembly_class=2)])
    idx = data_row_index(picker, Column.ASSEMBLY_CLASS)
    assert idx.data(Qt.ItemDataRole.ForegroundRole) is None


# --- repeat cue -------------------------------------------------------------

def test_repeat_column_uses_the_two_segment_delegate(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    delegate = picker.table_view.itemDelegateForColumn(Column.REPEAT)
    assert isinstance(delegate, RepeatReferenceDelegate)


def test_repeat_display_and_sort_use_the_joined_string(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate(
        [create_digest(repeat_meta={"rowc_label": "ROWC", "rowc_ref": "123456"})]
    )

    idx = data_row_index(picker, Column.REPEAT)
    assert idx.data(Qt.ItemDataRole.DisplayRole) == "ROWC 123456"
    assert "rowc 123456" in picker._searchable_text(
        create_digest(repeat_meta={"rowc_label": "ROWC", "rowc_ref": "123456"})
    )


def test_delegate_paints_group_headers_through_the_default_path(qtbot):
    """The span makes this unreachable; the guard must survive it changing."""
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    picker.populate([create_digest()])

    header_idx = None
    for row in range(picker.model.rowCount()):
        idx = picker.model.index(row, Column.REPEAT)
        payload = idx.data(Qt.ItemDataRole.UserRole)
        if payload and payload["kind"] == RowKind.GROUP_HEADER:
            header_idx = idx
            break
    assert header_idx is not None

    from PyQt6.QtGui import QPixmap, QPainter
    from PyQt6.QtWidgets import QStyleOptionViewItem

    pixmap = QPixmap(120, 24)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = pixmap.rect()
        picker.table_view.itemDelegateForColumn(Column.REPEAT).paint(
            painter, option, header_idx
        )
    finally:
        painter.end()


# --- header state -----------------------------------------------------------

def test_stale_header_blob_is_overwritten_not_merely_ignored(qtbot, tmp_path, caplog):
    """Finding 4: the fallback must be persisted, or every launch re-warns."""
    import logging

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("audit_list/header_state", b"not a header state")

    with caplog.at_level(logging.WARNING):
        first = OpenAuditPicker(settings=settings)
        qtbot.addWidget(first)
    assert any("header state failed to restore" in r.message for r in caplog.records)

    stored = settings.value("audit_list/header_state")
    assert stored not in (None, b"not a header state")

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        second = OpenAuditPicker(settings=settings)
        qtbot.addWidget(second)
    assert not any("header state failed to restore" in r.message for r in caplog.records)
