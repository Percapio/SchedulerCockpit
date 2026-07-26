import pytest
from PyQt6.QtCore import Qt
from cockpit.ui.canvas.page_switcher import PageSwitcher, SwitcherMode
from cockpit.ui.theme import Theme


def test_page_switcher_hidden_on_1_or_fewer_pages(qtbot):
    switcher = PageSwitcher()
    qtbot.addWidget(switcher)
    
    switcher.set_page_count(1)
    assert switcher.mode == SwitcherMode.HIDDEN
    assert switcher.isHidden()

    switcher.set_page_count(0)
    assert switcher.mode == SwitcherMode.HIDDEN
    assert switcher.isHidden()


def test_page_switcher_segmented_primary_2_pages(qtbot):
    switcher = PageSwitcher()
    qtbot.addWidget(switcher)
    
    switcher.set_page_count(2, is_reference=False)
    assert switcher.mode == SwitcherMode.SEGMENTED
    assert not switcher.isHidden()
    assert len(switcher.buttons) == 2
    assert switcher.buttons[0].text() == "Top"
    assert switcher.buttons[1].text() == "Bottom"


def test_page_switcher_segmented_reference_3_pages(qtbot):
    switcher = PageSwitcher()
    qtbot.addWidget(switcher)
    
    switcher.set_page_count(3, is_reference=True)
    assert switcher.mode == SwitcherMode.SEGMENTED
    assert not switcher.isHidden()
    assert len(switcher.buttons) == 3
    assert [b.text() for b in switcher.buttons] == ["Page 1", "Page 2", "Page 3"]


def test_page_switcher_pager_5_pages_and_stepping(qtbot):
    theme = Theme.for_testing(canvas={"page_switcher_segmented_max": 4})
    switcher = PageSwitcher(theme)
    qtbot.addWidget(switcher)
    
    signals = []
    switcher.page_changed.connect(signals.append)

    switcher.set_page_count(5, is_reference=True)
    assert switcher.mode == SwitcherMode.PAGER
    assert not switcher.isHidden()
    assert len(switcher.buttons) == 0
    
    assert switcher._pager_label.text() == "Page 1 / 5"
    assert not switcher._pager_prev.isEnabled()
    assert switcher._pager_next.isEnabled()

    # Step next
    switcher._pager_next.click()
    assert switcher._pager_label.text() == "Page 2 / 5"
    assert switcher._pager_prev.isEnabled()
    assert switcher._pager_next.isEnabled()
    assert signals == [1]

    # Step prev
    switcher._pager_prev.click()
    assert switcher._pager_label.text() == "Page 1 / 5"
    assert not switcher._pager_prev.isEnabled()
    assert signals == [1, 0]


def test_page_switcher_teardown_children_mode_transition(qtbot):
    theme = Theme.for_testing(canvas={"page_switcher_segmented_max": 4})
    switcher = PageSwitcher(theme)
    qtbot.addWidget(switcher)
    
    # Start in Pager mode (5 pages)
    switcher.set_page_count(5, is_reference=True)
    assert switcher.mode == SwitcherMode.PAGER
    assert switcher._pager_label is not None
    assert switcher.layout.count() == 3

    # Transition down to Segmented mode (2 pages)
    switcher.set_page_count(2, is_reference=False)
    assert switcher.mode == SwitcherMode.SEGMENTED
    assert switcher._pager_label is None
    assert switcher._pager_prev is None
    assert switcher._pager_next is None
    assert len(switcher.buttons) == 2
    assert switcher.layout.count() == 2
    assert switcher.buttons[0].text() == "Top"
    assert switcher.buttons[1].text() == "Bottom"
