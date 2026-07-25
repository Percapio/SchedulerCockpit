import pytest
from datetime import datetime, timezone
from PyQt6.QtCore import Qt, QModelIndex
from cockpit.ui.widgets.open_audit_picker import OpenAuditPicker, Column, RowKind
from cockpit.services.views import OpenAuditDigest
from cockpit.persistence.types import AuditStatus
from cockpit.ui import facelift


def create_digest(audit_id=1, status=AuditStatus.NOT_CLEAR, is_labeled=False, are_photos_uploaded=False):
    return OpenAuditDigest(
        audit_id=audit_id,
        part_number="PN-38",
        work_order_ref="WO-38",
        split_suffix="",
        quantity=10,
        status=status,
        updated_at=datetime.now(timezone.utc),
        date_ingested=datetime.now(timezone.utc),
        ship_date=None,
        lead_time_days=None,
        repeat="NEW",
        classification="Non-ITAR",
        assembly_class=2,
        process=None,
        feeder_setuptime=1.0,
        smt_runtime=2.0,
        tht_runtime=3.0,
        aoi_runtime=4.0,
        ops_runtime=5.0,
        shipping_runtime=6.0,
        start_by=None,
        is_labeled=is_labeled,
        are_photos_uploaded=are_photos_uploaded,
    )


def test_picker_columns_and_checkbox_flags(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    digest = create_digest(is_labeled=True, are_photos_uploaded=False)
    picker.populate([digest])

    assert len(picker.model.COLUMNS) == 19
    assert picker.model.COLUMNS[Column.LABEL] == "Label"
    assert picker.model.COLUMNS[Column.PHOTOS] == "Photos"

    # Row 0 is group header, Row 1 is data row
    data_row = 1
    label_idx = picker.model.index(data_row, Column.LABEL)
    photos_idx = picker.model.index(data_row, Column.PHOTOS)
    part_idx = picker.model.index(data_row, Column.PART_NUMBER)

    # Check flags
    label_flags = picker.model.flags(label_idx)
    assert label_flags & Qt.ItemFlag.ItemIsUserCheckable
    assert not (label_flags & Qt.ItemFlag.ItemIsSelectable)

    part_flags = picker.model.flags(part_idx)
    assert not (part_flags & Qt.ItemFlag.ItemIsUserCheckable)
    assert part_flags & Qt.ItemFlag.ItemIsSelectable

    # Check check state role
    assert picker.model.data(label_idx, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert picker.model.data(photos_idx, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked


def test_picker_checkbox_signals_and_click_guard(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)
    digest = create_digest(audit_id=99)
    picker.populate([digest])

    data_row = 1
    label_idx = picker.model.index(data_row, Column.LABEL)

    label_emitted = []
    photos_emitted = []
    selected_emitted = []

    picker.label_toggle_requested.connect(lambda a_id, val: label_emitted.append((a_id, val)))
    picker.photos_toggle_requested.connect(lambda a_id, val: photos_emitted.append((a_id, val)))
    picker.audit_selected.connect(selected_emitted.append)

    # Simulate toggle via setData
    picker.model.setData(label_idx, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert label_emitted == [(99, True)]
    assert photos_emitted == []

    # Simulate click on checkbox column - should be ignored by click guard
    picker.table_view.clicked.emit(label_idx)
    assert selected_emitted == []

    # Simulate click on part number column - should emit audit_selected
    part_idx = picker.model.index(data_row, Column.PART_NUMBER)
    picker.table_view.clicked.emit(part_idx)
    assert selected_emitted == [99]


def test_picker_status_stage_highlighting(qtbot):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)

    # Create digest in SMT status
    digest = create_digest(status=AuditStatus.SMT)
    picker.populate([digest])

    data_row = 1
    smt_idx = picker.model.index(data_row, Column.SMT)
    tht_idx = picker.model.index(data_row, Column.THT)

    smt_color = picker.model.data(smt_idx, Qt.ItemDataRole.ForegroundRole)
    tht_color = picker.model.data(tht_idx, Qt.ItemDataRole.ForegroundRole)

    assert smt_color == facelift.attention_color()
    assert tht_color != facelift.attention_color()
