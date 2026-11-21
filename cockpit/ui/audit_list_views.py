"""Saved column views for the Audit List (Phase 59 §6).

Qt-free: the layouts, their reconciliation with today's columns, the stored JSON, and the name
rules. The picker owns everything that touches a QHeaderView.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Sequence

from cockpit.services.requirements import Failure, Result, Success

logger = logging.getLogger(__name__)

MAX_SAVED_VIEWS: int = 9
MAX_VIEW_NAME_CHARS: int = 24
MIN_COLUMN_WIDTH_PX: int = 24
MAX_COLUMN_WIDTH_PX: int = 800
DEFAULT_VIEW_NAME: str = "Default"
VIEWS_SETTINGS_KEY: str = "audit_list/views"
VIEWS_FORMAT: int = 1
LEGACY_HEADER_STATE_KEY: str = "audit_list/header_state"
LEGACY_HEADER_COLUMNS_KEY: str = "audit_list/header_state_columns"
LEGACY_COLUMN_COUNT: int = 24


@dataclass(frozen=True)
class ColumnSpec:
    """One Audit List column. key is the Column member's name, which is what views store."""

    key: str
    title: str
    default_width_px: int
    default_visible: bool = True
    hideable: bool = True


@dataclass(frozen=True)
class ColumnPlacement:
    key: str
    width_px: int
    visible: bool


@dataclass(frozen=True)
class ColumnLayout:
    """Every registry column once, in visual order, the registry's first column first and visible."""

    placements: tuple[ColumnPlacement, ...]

    def keys(self) -> list[str]:
        return [placement.key for placement in self.placements]

    def placement(self, key: str) -> ColumnPlacement | None:
        return next((placement for placement in self.placements if placement.key == key), None)

    def is_visible(self, key: str) -> bool:
        placement = self.placement(key)
        return placement is not None and placement.visible


@dataclass(frozen=True)
class SavedView:
    name: str
    layout: ColumnLayout


@dataclass(frozen=True)
class SavedViews:
    """1..MAX_SAVED_VIEWS views in creation order, names unique ignoring case; active names one."""

    views: tuple[SavedView, ...]
    active: str

    def names(self) -> list[str]:
        return [view.name for view in self.views]

    def find(self, name: str) -> SavedView | None:
        folded = name.strip().casefold()
        return next((view for view in self.views if view.name.casefold() == folded), None)

    def active_view(self) -> SavedView:
        return self.find(self.active) or self.views[0]

    def with_active(self, name: str) -> "SavedViews":
        view = self.find(name)
        return self if view is None else replace(self, active=view.name)

    def with_layout(self, name: str, layout: ColumnLayout) -> "SavedViews":
        folded = name.casefold()
        return replace(self, views=tuple(
            replace(view, layout=layout) if view.name.casefold() == folded else view for view in self.views
        ))

    def added(self, view: SavedView) -> "SavedViews":
        return SavedViews(self.views + (view,), view.name)

    def renamed(self, old: str, new: str) -> "SavedViews":
        folded = old.casefold()
        views = tuple(replace(view, name=new) if view.name.casefold() == folded else view for view in self.views)
        active = new if self.active.casefold() == folded else self.active
        return SavedViews(views, active)

    def removed(self, name: str) -> "SavedViews":
        folded = name.casefold()
        views = tuple(view for view in self.views if view.name.casefold() != folded)
        if not views:
            return self
        active = self.active if self.active.casefold() != folded else views[0].name
        return SavedViews(views, active)


# ---------- name rules ----------

@dataclass(frozen=True)
class Blank:
    pass


@dataclass(frozen=True)
class TooLong:
    limit: int


@dataclass(frozen=True)
class Duplicate:
    name: str


@dataclass(frozen=True)
class LimitReached:
    limit: int


ViewNameRefused = Blank | TooLong | Duplicate | LimitReached


def view_name_refusal_text(refusal: ViewNameRefused) -> str:
    if isinstance(refusal, Blank):
        return "Give the view a name."
    if isinstance(refusal, TooLong):
        return f"A view's name can be {refusal.limit} characters at most."
    if isinstance(refusal, Duplicate):
        return f"A view named {refusal.name} already exists."
    return "Nine views is the limit; delete one first."


def validate_view_name(
    candidate: str,
    views: SavedViews,
    renaming: Optional[str] = None,
) -> Result[str, ViewNameRefused]:
    """Checks a name for a new or renamed view (§6.4)."""
    name = (candidate or "").strip()
    if not name:
        return Failure(Blank())
    if len(name) > MAX_VIEW_NAME_CHARS:
        return Failure(TooLong(MAX_VIEW_NAME_CHARS))
    folded = name.casefold()
    renamed_folded = renaming.casefold() if renaming is not None else None
    for view in views.views:
        if view.name.casefold() == folded and view.name.casefold() != renamed_folded:
            return Failure(Duplicate(view.name))
    if renaming is None and len(views.views) >= MAX_SAVED_VIEWS:
        return Failure(LimitReached(MAX_SAVED_VIEWS))
    return Success(name)


# ---------- layouts ----------

def clamp_width(width_px: int) -> int:
    return max(MIN_COLUMN_WIDTH_PX, min(MAX_COLUMN_WIDTH_PX, int(width_px)))


def default_layout(registry: Sequence[ColumnSpec]) -> ColumnLayout:
    return ColumnLayout(tuple(
        ColumnPlacement(spec.key, clamp_width(spec.default_width_px), spec.default_visible or not spec.hideable)
        for spec in registry
    ))


def default_views(registry: Sequence[ColumnSpec]) -> SavedViews:
    return SavedViews((SavedView(DEFAULT_VIEW_NAME, default_layout(registry)),), DEFAULT_VIEW_NAME)


def reconcile_layout(stored: Sequence[ColumnPlacement], registry: Sequence[ColumnSpec]) -> ColumnLayout:
    """Fits a stored layout to today's columns (§6.2), in the order the contract states."""
    specs = {spec.key: spec for spec in registry}
    working: list[ColumnPlacement] = []
    seen: set[str] = set()
    for placement in stored:
        if placement.key not in specs or placement.key in seen:
            continue
        seen.add(placement.key)
        working.append(placement)

    pinned = registry[0]
    index = next((i for i, placement in enumerate(working) if placement.key == pinned.key), None)
    if index is None:
        working.insert(0, ColumnPlacement(pinned.key, pinned.default_width_px, True))
    else:
        working.insert(0, replace(working.pop(index), visible=True))

    present = {placement.key for placement in working}
    for position, spec in enumerate(registry):
        if spec.key in present:
            continue
        at = 0
        for earlier in reversed(registry[:position]):
            if earlier.key in present:
                at = next(i for i, placement in enumerate(working) if placement.key == earlier.key) + 1
                break
        working.insert(at, ColumnPlacement(spec.key, spec.default_width_px, spec.default_visible))
        present.add(spec.key)

    return ColumnLayout(tuple(
        replace(placement, width_px=clamp_width(placement.width_px),
                visible=placement.visible or not specs[placement.key].hideable)
        for placement in working
    ))


# ---------- storage ----------

def views_to_json(views: SavedViews) -> str:
    return json.dumps({
        "format": VIEWS_FORMAT,
        "active": views.active,
        "views": [
            {
                "name": view.name,
                "columns": [
                    {"key": placement.key, "width": placement.width_px, "visible": placement.visible}
                    for placement in view.layout.placements
                ],
            }
            for view in views.views
        ],
    }, ensure_ascii=False, separators=(",", ":"))


def _parse_placement(raw: Any) -> ColumnPlacement | None:
    if not isinstance(raw, dict):
        return None
    key, width, visible = raw.get("key"), raw.get("width"), raw.get("visible")
    if not isinstance(key, str) or type(width) is not int or type(visible) is not bool:
        return None
    return ColumnPlacement(key, width, visible)


def _parse_view(raw: Any, registry: Sequence[ColumnSpec]) -> SavedView | None:
    if not isinstance(raw, dict):
        return None
    name, columns = raw.get("name"), raw.get("columns")
    if not isinstance(name, str) or not isinstance(columns, list):
        return None
    name = name.strip()
    if not name or len(name) > MAX_VIEW_NAME_CHARS:
        return None
    placements = [placement for placement in map(_parse_placement, columns) if placement is not None]
    return SavedView(name, reconcile_layout(placements, registry))


def parse_views(raw: Any, registry: Sequence[ColumnSpec]) -> SavedViews | None:
    """The stored views, or None when the value doesn't parse, is newer, or leaves no valid view."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    stored_format = payload.get("format")
    if type(stored_format) is not int or not 1 <= stored_format <= VIEWS_FORMAT:
        return None
    raw_views = payload.get("views")
    if not isinstance(raw_views, list):
        return None
    views: list[SavedView] = []
    seen: set[str] = set()
    for entry in raw_views:
        if len(views) >= MAX_SAVED_VIEWS:
            break
        view = _parse_view(entry, registry)
        if view is None or view.name.casefold() in seen:
            continue
        seen.add(view.name.casefold())
        views.append(view)
    if not views:
        return None
    active = payload.get("active")
    chosen = views[0].name
    if isinstance(active, str):
        match = next((view for view in views if view.name.casefold() == active.strip().casefold()), None)
        if match is not None:
            chosen = match.name
    return SavedViews(tuple(views), chosen)


def _legacy_column_count(settings: Any) -> int | None:
    try:
        return int(settings.value(LEGACY_HEADER_COLUMNS_KEY))
    except (TypeError, ValueError):
        return None


def store_views(settings: Any, views: SavedViews) -> None:
    if settings is not None:
        settings.setValue(VIEWS_SETTINGS_KEY, views_to_json(views))


def load_views(
    settings: Any,
    registry: Sequence[ColumnSpec],
    decode_legacy: Callable[[Any], ColumnLayout | None],
) -> SavedViews:
    """Loads the Audit List's views, carrying the pre-Phase 59 layout over once (§6.2).

    settings is a QSettings or anything with value, setValue and remove; None keeps the views in
    memory and reads and writes nothing.
    """
    if settings is None:
        return default_views(registry)

    raw = settings.value(VIEWS_SETTINGS_KEY)
    if raw is None:
        layout: ColumnLayout | None = None
        blob = settings.value(LEGACY_HEADER_STATE_KEY)
        if blob is not None:
            if _legacy_column_count(settings) == LEGACY_COLUMN_COUNT:
                try:
                    layout = decode_legacy(blob)
                except Exception:
                    logger.warning("Audit list: the saved column layout couldn't be decoded")
                    layout = None
            if layout is None:
                logger.warning("Audit list: the saved column layout couldn't be carried over; using the default columns")
        placements = layout.placements if layout is not None else ()
        views = SavedViews(
            (SavedView(DEFAULT_VIEW_NAME, reconcile_layout(placements, registry)),), DEFAULT_VIEW_NAME,
        )
        settings.remove(LEGACY_HEADER_STATE_KEY)
        settings.remove(LEGACY_HEADER_COLUMNS_KEY)
    else:
        parsed = parse_views(raw, registry)
        if parsed is None:
            logger.warning("Audit list: %s couldn't be read; using the default view", VIEWS_SETTINGS_KEY)
            views = default_views(registry)
        else:
            views = parsed
    store_views(settings, views)
    return views
