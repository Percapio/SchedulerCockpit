"""F4 -- cycle-free row wiring (Optimize06 section 5, step 4 gate).

The gate is "live counts fall at the same cycle they are dropped, not one gen-2
collection later". Whole-application A1 cannot show that: the churn cycle runs
gc.collect() before reading, and a collected cycle is indistinguishable from no
cycle. So these tests assert reclamation by REFCOUNT ALONE, with the cyclic
collector disabled, per row class.

test_negative_control_a_real_cycle_survives_refcount is the load-bearing test in
this file. Without it, every assertion below could be passing because the weakref
instrument is blind rather than because the cycles are gone -- the same failure
mode that let F1 through F4 accumulate behind a green fixture.
"""

import gc
import weakref
from typing import Callable
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QPushButton, QVBoxLayout, QWidget

from cockpit.services.layout_query import AuditBomRowView
from cockpit.services.views import ChecklistRowKey, ChecklistRowKind, ChecklistRowView
from cockpit.ui.canvas.page_switcher import PageSwitcher, SwitcherMode
from cockpit.ui.theme import Theme
from cockpit.ui.widgets.audit_bom_panel import AuditBomRow
from cockpit.ui.widgets.component_row import ClickableLabel, ComponentRowCore, ComponentRowFields
from cockpit.ui.widgets.refdes_chip import RefDesChip


@pytest.fixture
def theme():
    return Theme.for_testing(
        checklist_panel={
            "row": {
                "corner_radius_px": 4,
                "vertical_padding_px": 6,
                "horizontal_padding_px": 8,
                "gutter_px": 3,
            },
        },
        bom_panel={
            "chip": {
                "corner_radius_px": 3,
                "vertical_padding_px": 3,
                "horizontal_padding_px": 6,
                "flow_spacing_px": 4,
            },
        },
    )


@pytest.fixture
def no_cycle_collector():
    """Refcount only. An automatic collection would reclaim a cycle and make
    every assertion in this file pass for the wrong reason."""
    was_enabled = gc.isenabled()
    gc.disable()
    gc.collect()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def dropped_ref(factory: Callable[[], object]) -> weakref.ref:
    """Build one object, weakly reference it, and drop the only strong ref.

    The strong reference lives in this frame's locals and dies with the frame,
    so the caller holds nothing but the weakref.
    """
    obj = factory()
    ref = weakref.ref(obj)
    del obj
    return ref


def _tht_row_view() -> ChecklistRowView:
    return ChecklistRowView(
        key=ChecklistRowKey(kind=ChecklistRowKind.THT, item_id=1),
        primary_label="MPN-1",
        secondary_label="a description",
        find_number=1,
        ref_des_list=("R1", "R2", "C7"),
    )


def _bom_row_view() -> AuditBomRowView:
    return AuditBomRowView(
        find_number=1,
        component_mpn="MPN-1",
        description="a description",
        mount_type="S",
        ref_des_list=("R1", "R2", "C7"),
    )


def _core_fields() -> ComponentRowFields:
    return ComponentRowFields(
        find_number=1,
        mpn="MPN-1",
        description="a description",
        ref_des_list=("R1", "R2", "C7"),
    )


# ---------------------------------------------------------------------------
# Negative control -- run this first mentally, and believe nothing below it
# unless this passes.
# ---------------------------------------------------------------------------

class DeliberatelyCyclicRow(QWidget):
    """The exact topology section 5 bans: a lambda closing over self, connected
    to a signal on a child of self."""

    activated = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.button = QPushButton("x", self)
        self.button.clicked.connect(lambda: self.activated.emit())
        layout.addWidget(self.button)


def test_negative_control_a_real_cycle_survives_refcount(qapp, no_cycle_collector):
    ref = dropped_ref(DeliberatelyCyclicRow)

    assert ref() is not None, (
        "The weakref instrument cannot see a reference cycle. Every other "
        "assertion in this file is meaningless until this is fixed."
    )

    gc.collect()
    assert ref() is None, "the cycle should be collectable, just not by refcount"


# ---------------------------------------------------------------------------
# The four F4 sites
# ---------------------------------------------------------------------------

def test_component_row_core_is_reclaimed_by_refcount(qapp, no_cycle_collector, theme):
    """Site 1: MPNLabelFilter held self.row back at the core, through a filter
    parented to a child of the core."""
    ref = dropped_ref(lambda: ComponentRowCore(_core_fields(), theme))

    assert ref() is None




def test_audit_bom_row_is_reclaimed_by_refcount(qapp, no_cycle_collector, theme):
    """Site 2: both mpn_label_clicked and refdes_chip_clicked were lambdas."""
    ref = dropped_ref(lambda: AuditBomRow(_bom_row_view(), theme))

    assert ref() is None


def test_page_switcher_is_reclaimed_by_refcount(qapp, no_cycle_collector, theme):
    """Site 4, found during the R2 disposition: set_page_count connected each
    segment button's clicked to a lambda closing over the switcher, rebuilt on
    every page-count change."""

    def build() -> PageSwitcher:
        switcher = PageSwitcher(theme)
        switcher.set_page_count(3)
        return switcher

    ref = dropped_ref(build)

    assert ref() is None


def test_page_switcher_survives_repeated_page_count_changes(qapp, no_cycle_collector, theme):
    """The cycle was rebuilt per page-count change, so one rebuild is not a
    sufficient exercise of the site."""

    def build() -> PageSwitcher:
        switcher = PageSwitcher(theme)
        for count in (2, 3, 8, 2, 5):
            switcher.set_page_count(count)
        return switcher

    ref = dropped_ref(build)

    assert ref() is None


def test_mpn_label_filter_is_gone():
    """The class the site-1 cycle lived in. Section 5 deletes it rather than
    rewiring it."""
    import cockpit.ui.widgets.refdes_chip as refdes_chip

    assert not hasattr(refdes_chip, "MPNLabelFilter")


# ---------------------------------------------------------------------------
# Behaviour preserved -- section 5 is "mechanical, no behaviour change"
# ---------------------------------------------------------------------------

def test_clickable_label_emits_and_consumes_left_click(qtbot):
    label = ClickableLabel("MPN-1")
    qtbot.addWidget(label)
    fired: list[bool] = []
    label.clicked.connect(lambda: fired.append(True))

    qtbot.mouseClick(label, Qt.MouseButton.LeftButton)

    assert fired == [True]


def test_core_left_click_on_mpn_emits_mpn_label_clicked(qtbot, theme):
    core = ComponentRowCore(_core_fields(), theme)
    qtbot.addWidget(core)
    emitted: list[str] = []
    core.mpn_label_clicked.connect(emitted.append)

    qtbot.mouseClick(core.mpn_label, Qt.MouseButton.LeftButton)

    assert emitted == ["MPN-1"]


def test_core_chip_click_emits_refdes_chip_clicked(qtbot, theme):
    core = ComponentRowCore(_core_fields(), theme)
    qtbot.addWidget(core)
    emitted: list[str] = []
    core.refdes_chip_clicked.connect(emitted.append)

    qtbot.mouseClick(core.chips["R2"], Qt.MouseButton.LeftButton)

    assert emitted == ["R2"]


def test_audit_bom_row_reports_its_own_mpn_not_the_signal_argument(qtbot, theme):
    """The bound method ignores the incoming ref_des; the row's own view is
    authoritative, exactly as the lambda it replaced was."""
    row = AuditBomRow(_bom_row_view(), theme)
    qtbot.addWidget(row)
    emitted: list[str] = []
    row.row_clicked.connect(emitted.append)

    row.core.refdes_chip_clicked.emit("R2")
    row.core.mpn_label_clicked.emit("something-else")

    assert emitted == ["MPN-1", "MPN-1"]



def test_page_switcher_segment_click_reports_the_right_index(qtbot, theme):
    """The index is recovered from the sender rather than from a per-button
    default argument. That recovery is what removed the closure, so it is worth
    pinning that it recovers the correct index."""
    switcher = PageSwitcher(theme)
    qtbot.addWidget(switcher)
    switcher.set_page_count(3)
    emitted: list[int] = []
    switcher.page_changed.connect(emitted.append)

    switcher.buttons[2].click()

    assert emitted == [2]
    assert switcher._current_index == 2


def test_page_switcher_teardown_disconnects_before_destroying(qtbot, theme):
    """teardown_children() deleteLater()'d the buttons without severing them, so
    the closure outlived the C++ object."""
    switcher = PageSwitcher(theme)
    qtbot.addWidget(switcher)
    switcher.set_page_count(3)
    button = switcher.buttons[1]
    emitted: list[int] = []
    switcher.page_changed.connect(emitted.append)

    switcher.teardown_children()
    button.clicked.emit()

    assert emitted == []
    assert switcher.buttons == []


def test_page_switcher_reset_does_not_emit_page_changed(qtbot, theme):
    """Section 3.4's post-condition, and the precondition for invariant I3:
    reset() runs inside LayoutCanvas.unload()."""
    switcher = PageSwitcher(theme)
    qtbot.addWidget(switcher)
    switcher.set_page_count(3)
    switcher.buttons[2].click()
    emitted: list[int] = []
    switcher.page_changed.connect(emitted.append)

    switcher.reset()

    assert emitted == []
    assert switcher.mode is SwitcherMode.HIDDEN
    assert switcher._page_count == 0
    assert switcher._current_index == 0


def test_refdes_chip_click_carries_its_ref_des(qtbot):
    chip = RefDesChip("C7")
    qtbot.addWidget(chip)
    emitted: list[str] = []
    chip.clicked.connect(emitted.append)

    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)

    assert emitted == ["C7"]
