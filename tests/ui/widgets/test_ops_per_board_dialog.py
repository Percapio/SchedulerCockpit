import pytest
from PyQt6.QtWidgets import QPushButton
from cockpit.ui.widgets.dialogs import OpsPerBoardDialog
from cockpit.ui.widgets.open_audit_picker import OpenAuditPicker
from cockpit.services.views import OpenAuditDigest
from cockpit.persistence.types import AuditStatus
from datetime import datetime, timezone


def test_ops_per_board_dialog_init_and_save(qtbot):
    dlg = OpsPerBoardDialog(current=2.5)
    qtbot.addWidget(dlg)
    assert dlg.spin_box.value() == 2.5
    assert not dlg.was_cleared()
    assert dlg.result_value() == 2.5

    # Test changing value and saving
    dlg.spin_box.setValue(3.75)
    save_btn = next(btn for btn in dlg.findChildren(QPushButton) if btn.text() == "Save")
    save_btn.click()
    assert dlg.result_value() == 3.75
    assert not dlg.was_cleared()


def test_ops_per_board_dialog_init_none_and_clear(qtbot):
    dlg = OpsPerBoardDialog(current=None)
    qtbot.addWidget(dlg)
    assert dlg.spin_box.value() == 0.0
    assert not dlg.was_cleared()
    assert dlg.result_value() == 0.0

    clear_btn = next(btn for btn in dlg.findChildren(QPushButton) if btn.text() == "Clear")
    clear_btn.click()
    assert dlg.was_cleared()
    assert dlg.result_value() is None


def test_open_audit_picker_ops_action_signal(qtbot, monkeypatch):
    picker = OpenAuditPicker()
    qtbot.addWidget(picker)

    digest = OpenAuditDigest(
        audit_id=42,
        part_number="P-123",
        work_order_ref="WO-1",
        split_suffix="",
        quantity=10,
        status=AuditStatus.NOT_CLEAR,
        updated_at=datetime.now(timezone.utc),
        date_ingested=datetime.now(timezone.utc),
        ship_date=None,
        lead_time_days=None,
        repeat=None,
        classification="Class 2",
        assembly_class=2,
        process="Clean",
        feeder_setuptime=None,
        smt_runtime=None,
        tht_runtime=None,
        aoi_runtime=None,
        ops_runtime=None,
        shipping_runtime=None,
        start_by=None,
        ops_per_board_min=1.5,
    )

    emitted = []
    picker.ops_per_board_change_requested.connect(lambda a_id, val: emitted.append((a_id, val)))

    # Monkeypatch dialog in open_audit_picker
    class MockOpsDialog:
        def __init__(self, current, parent=None):
            self.current = current
        def exec(self):
            return True
        def result_value(self):
            return 4.25

    monkeypatch.setattr("cockpit.ui.widgets.dialogs.OpsPerBoardDialog", MockOpsDialog)
    picker._on_ops_action(digest)

    assert emitted == [(42, 4.25)]
