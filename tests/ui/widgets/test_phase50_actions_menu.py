"""Phase 50 sections 5.1-5.3 and 4.3 — the ellipsis menu."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QSettings

from cockpit.settings.source_root import SourceRootController
from cockpit.ui.widgets.audit_actions_bar import AuditActionsBar


def make_view(has_pdf=False, has_secondary_pdf=False, part_number="B123456"):
    return SimpleNamespace(
        audit_id=1,
        part_number=part_number,
        has_pdf=has_pdf,
        has_secondary_pdf=has_secondary_pdf,
        status="Not Clear",
    )


@pytest.fixture
def make_bar(qtbot, tmp_path):
    def build(view, source_root=None):
        settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        controller = SourceRootController(settings)
        if source_root is not None:
            controller.set_source_root(str(source_root))
        bar = AuditActionsBar(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
            source_root_controller=controller,
        )
        qtbot.addWidget(bar)
        session = MagicMock()
        session.current_view.return_value = view
        bar.bind(session)
        bar._rebuild_actions_menu()
        return bar

    return build


def top_level(bar):
    return [a.text() for a in bar.actions_menu.actions()]


def submenu_named(bar, label):
    for action in bar.actions_menu.actions():
        if action.text() == label:
            return action.menu()
    return None


# --- section 5.1, shape -----------------------------------------------------

def test_print_submenu_holds_both_sheets(make_bar, tmp_path):
    bar = make_bar(make_view(has_pdf=True), source_root=tmp_path)
    print_menu = submenu_named(bar, "Print")
    assert print_menu is not None
    assert [a.text() for a in print_menu.actions()] == ["Release form…", "Setup sheet…"]


def test_remaining_actions_stay_flat(make_bar, tmp_path):
    bar = make_bar(make_view(has_pdf=True), source_root=tmp_path)
    labels = top_level(bar)
    assert "Split" in labels
    assert "2nd OPS…" in labels
    assert "OPS per board…" in labels
    # The old flat entries are gone.
    assert "Release…" not in labels
    assert "Setup…" not in labels
    assert "Replace" not in labels


def test_each_role_menu_offers_fetch_and_manual(make_bar, tmp_path):
    bar = make_bar(make_view(), source_root=tmp_path)
    add_menu = submenu_named(bar, "Add")
    drawing = add_menu.actions()[0].menu()
    assert [a.text() for a in drawing.actions()] == ["Fetch", "Manual"]


# --- section 5.2, membership ------------------------------------------------

def test_no_drawings_means_add_only(make_bar, tmp_path):
    bar = make_bar(make_view(), source_root=tmp_path)
    labels = top_level(bar)
    assert "Add" in labels
    assert "Update" not in labels
    add_menu = submenu_named(bar, "Add")
    assert [a.text() for a in add_menu.actions()] == ["Drawing", "Secondary Drawing"]


def test_both_drawings_means_update_only(make_bar, tmp_path):
    bar = make_bar(make_view(has_pdf=True, has_secondary_pdf=True), source_root=tmp_path)
    labels = top_level(bar)
    assert "Add" not in labels
    assert "Update" in labels
    update_menu = submenu_named(bar, "Update")
    assert [a.text() for a in update_menu.actions()] == ["Drawing", "Secondary Drawing"]


def test_a_role_never_appears_under_both(make_bar, tmp_path):
    bar = make_bar(make_view(has_pdf=True, has_secondary_pdf=False), source_root=tmp_path)
    add_roles = [a.text() for a in submenu_named(bar, "Add").actions()]
    update_roles = [a.text() for a in submenu_named(bar, "Update").actions()]
    assert add_roles == ["Secondary Drawing"]
    assert update_roles == ["Drawing"]
    assert not set(add_roles) & set(update_roles)


# --- section 4.3, Fetch availability ----------------------------------------

def fetch_action(bar, menu_label):
    role_menu = submenu_named(bar, menu_label).actions()[0].menu()
    return role_menu.actions()[0]


def test_fetch_enabled_with_a_configured_root_and_job_number(make_bar, tmp_path):
    bar = make_bar(make_view(), source_root=tmp_path)
    action = fetch_action(bar, "Add")
    assert action.text() == "Fetch"
    assert action.isEnabled() is True


def test_fetch_disabled_when_the_root_is_unset(make_bar):
    bar = make_bar(make_view(), source_root=None)
    action = fetch_action(bar, "Add")
    assert action.isEnabled() is False
    assert "not configured" in action.toolTip()


def test_fetch_disabled_when_the_root_is_malformed(make_bar):
    bar = make_bar(make_view(), source_root="relative/path")
    action = fetch_action(bar, "Add")
    assert action.isEnabled() is False
    assert "not a valid path" in action.toolTip()


def test_fetch_disabled_when_part_number_is_not_a_job_number(make_bar, tmp_path):
    bar = make_bar(make_view(part_number="MANUAL-1"), source_root=tmp_path)
    action = fetch_action(bar, "Add")
    assert action.isEnabled() is False
    assert "not a job number" in action.toolTip()


@pytest.mark.parametrize("root", [None, "relative/path"])
def test_manual_stays_enabled_whenever_fetch_is_not(make_bar, root):
    bar = make_bar(make_view(), source_root=root)
    role_menu = submenu_named(bar, "Add").actions()[0].menu()
    manual = role_menu.actions()[1]
    assert manual.text() == "Manual"
    assert manual.isEnabled() is True


def test_fetch_emits_the_request_with_the_role(make_bar, tmp_path, qtbot):
    bar = make_bar(make_view(), source_root=tmp_path)
    add_menu = submenu_named(bar, "Add")
    secondary_menu = add_menu.actions()[1].menu()

    with qtbot.waitSignal(bar.drawing_fetch_requested) as blocker:
        secondary_menu.actions()[0].trigger()

    assert blocker.args == [1, True]


# --- section 5.3, submenu lifetime ------------------------------------------

def test_repeated_rebuilds_create_no_additional_submenus(make_bar, tmp_path):
    from PyQt6.QtWidgets import QMenu

    bar = make_bar(make_view(has_pdf=True), source_root=tmp_path)
    before = len(bar.findChildren(QMenu))

    for _ in range(50):
        bar._rebuild_actions_menu()

    assert len(bar.findChildren(QMenu)) == before
