"""The "Flag for {B#}" submenu (Phase 59 §4.5), shared by the Audit List and the Audit View's ⋯ menu.

Each action carries its (part number, kind) as data, and the owner connects the submenu's
triggered(QAction) to a bound method once. No per-action lambda holds the owner, so the submenu adds
no reference cycle through a C++ connection that only the garbage collector could break.
"""

from __future__ import annotations

from typing import Mapping

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu

from cockpit.services.requirements import BadgeState, RequirementKind, RequirementSummary, badge_state_of

FLAG_ACTIONS: tuple[tuple[RequirementKind, str], ...] = (
    (RequirementKind.AQUANOX, "Aquanox required"),
    (RequirementKind.PRESS_FIT, "Press-fit required"),
)
DOCUMENTS_ALREADY_REQUIRE = "The documents already require it; your flag keeps it if they stop"


def flag_menu_title(part_number: str) -> str:
    """The title names the part number, because a flag covers every open audit of it."""
    return f"Flag for {part_number}"


def populate_hand_flag_menu(
    menu: QMenu,
    part_number: str,
    hand_flags: Mapping[RequirementKind, object],
    requirements: RequirementSummary | None,
) -> None:
    """Fills menu with one checkable action per flaggable kind, checked iff your flag exists.

    post: menu holds exactly the two actions, each carrying (part_number, kind value) as its data; an
          action whose requirement the documents already detect carries DOCUMENTS_ALREADY_REQUIRE as
          its tooltip
    """
    menu.clear()
    menu.setTitle(flag_menu_title(part_number))
    menu.setToolTipsVisible(True)
    for kind, label in FLAG_ACTIONS:
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(kind in hand_flags)
        action.setData((part_number, kind.value))
        if badge_state_of(requirements, kind) == BadgeState.DETECTED:
            action.setToolTip(DOCUMENTS_ALREADY_REQUIRE)


def flag_request_of(action: QAction) -> tuple[str, str, bool] | None:
    """(part number, kind value, flagged) for a triggered flag action; None for any other action."""
    payload = action.data()
    if not isinstance(payload, tuple) or len(payload) != 2:
        return None
    part_number, kind_value = payload
    return str(part_number), str(kind_value), bool(action.isChecked())
