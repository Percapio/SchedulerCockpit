"""X-ray mode's markers, legend and hint, worked out without Qt (Phase 58 §6.4–§6.6).

The canvas hands layout_markers what it holds: the audit's X-ray view, every BOM designator's located
shapes by page, the displayed page and whether the stored scan is stale. Everything it shows comes back
as values, so the rules are tested without a window.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping, Sequence

from cockpit.placements.geometry import PartHighlight
from cockpit.services.requirements import (
    RequirementKind,
    RequirementState,
    StoredLocations,
    StoredXray,
)
from cockpit.utils.sorting import natural_sort_key

LEGEND_EXCERPTS_SHOWN = 3
# §6.5: when the legend is taller than its room, lines go in this order.
CUT_LATER_EXCERPTS = 1
CUT_FIRST_EXCERPT = 2
CUT_CONDITIONS = 3


@dataclass(frozen=True)
class XrayView:
    """What X-ray mode shows for one audit: its stored requirements and its BOM's designators (§6.3)."""

    stored: StoredXray
    bom_designators: frozenset[str]


@dataclass(frozen=True)
class Viewed:
    view: XrayView


@dataclass(frozen=True)
class NoScan:
    """The audit has no requirements row."""


@dataclass(frozen=True)
class ViewFailed:
    reason: str


XrayViewReading = Viewed | NoScan | ViewFailed


class MarkerKind(StrEnum):
    NAMED = "named"
    CLASS_ONLY = "class_only"


@dataclass(frozen=True)
class Marker:
    designator: str
    kind: MarkerKind
    shape: PartHighlight


@dataclass(frozen=True)
class LegendLine:
    """One legend line. cut_rank orders what goes when the legend is too tall (lower first, never when
    absent); a cut leaves short_text, or removes the line when there is none."""

    text: str
    cut_rank: int | None = None
    short_text: str | None = None


@dataclass(frozen=True)
class MarkerLayout:
    markers: tuple[Marker, ...]
    legend_lines: tuple[LegendLine, ...]
    hint: str | None
    other_pages: bool


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


def _unknown_reasons(stored: StoredXray) -> list[str]:
    reasons: list[str] = []
    for kind, state in ((RequirementKind.XRAY_SAVE_IMAGES, stored.save_images), (RequirementKind.XRAY, stored.xray)):
        if state == RequirementState.UNKNOWN:
            reason = stored.unknown_reasons.get(kind) or "unknown"
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def _shapes_by_designator(
    shown: Mapping[int, Sequence[PartHighlight]],
    wanted: Iterable[str],
) -> dict[str, list[PartHighlight]]:
    wanted_set = set(wanted)
    found: dict[str, list[PartHighlight]] = {}
    for page_shapes in shown.values():
        for shape in page_shapes:
            if shape.designator in wanted_set:
                found.setdefault(shape.designator, []).append(shape)
    return found


def _class_lines(locations) -> list[LegendLine]:
    """§6.5 line 4: one line per rule, 0 included, its matches' designators unioned and their distinct
    conditions joined; then the class error, once."""
    order: list[str] = []
    members: dict[str, set[str]] = {}
    conditions: dict[str, list[str]] = {}
    for match in locations.class_matches:
        if match.rule not in members:
            order.append(match.rule)
            members[match.rule] = set()
            conditions[match.rule] = []
        members[match.rule].update(match.designators)
        if match.condition and match.condition not in conditions[match.rule]:
            conditions[match.rule].append(match.condition)
    lines: list[LegendLine] = []
    for rule in order:
        short = f"{rule}: {len(members[rule]):,}"
        if conditions[rule]:
            lines.append(LegendLine(
                f"{short}, condition not checked: {'; '.join(conditions[rule])}", CUT_CONDITIONS, short,
            ))
        else:
            lines.append(LegendLine(short))
    if locations.class_notes_error:
        lines.append(LegendLine(f"Class notes not resolved: {locations.class_notes_error}"))
    return lines


def _unresolved_lines(locations) -> list[LegendLine]:
    """§6.5 line 6: the first three instructions that name no part, then how many more."""
    unresolved = locations.unresolved
    if not unresolved:
        return []
    lines = [LegendLine(f"In the documents, not tied to parts ({len(unresolved):,}):")]
    for index, instruction in enumerate(unresolved[:LEGEND_EXCERPTS_SHOWN]):
        rank = CUT_FIRST_EXCERPT if index == 0 else CUT_LATER_EXCERPTS
        lines.append(LegendLine(f"  “{instruction.excerpt}” ({instruction.location})", rank))
    if len(unresolved) > LEGEND_EXCERPTS_SHOWN:
        lines.append(LegendLine(f"  and {len(unresolved) - LEGEND_EXCERPTS_SHOWN:,} more", CUT_LATER_EXCERPTS))
    return lines


def layout_markers(
    reading: XrayViewReading,
    shown: Mapping[int, Sequence[PartHighlight]],
    page_index: int,
    is_stale: bool,
) -> MarkerLayout:
    """Lays out the displayed page's markers, the legend and the hint (Phase 58 §6.4–§6.6).

    post: markers are empty unless reading is Viewed with a Stored reading; then each location
          designator in the BOM gets one Marker per shape on page_index, NAMED when it is named and
          CLASS_ONLY otherwise; other_pages is true iff a drawable location has a shape on another page;
          the legend shows only for a Stored reading that holds something; the hint carries every other
          state, and "none on this page" when drawable locations sit only on other pages
    """
    if isinstance(reading, ViewFailed):
        return MarkerLayout((), (), "X-ray mode: X-ray locations couldn't be read", False)
    if isinstance(reading, NoScan):
        return MarkerLayout((), (), "X-ray mode: not scanned yet", False)
    view = reading.view
    if not isinstance(view.stored.reading, StoredLocations):
        return MarkerLayout((), (), "X-ray mode: rescan pending", False)

    locations = view.stored.reading.locations
    unknown = _unknown_reasons(view.stored)
    if locations.is_empty:
        if unknown:
            hint = f"X-ray mode: {'; '.join(unknown)}"
        else:
            hint = "X-ray mode: no X-ray locations for this audit"
        if is_stale:
            hint += " (rescan pending)"
        return MarkerLayout((), (), hint, False)

    named = locations.named
    every = locations.all_designators
    in_bom = every & view.bom_designators
    shapes = _shapes_by_designator(shown, in_bom)
    drawable = {designator for designator in in_bom if shapes.get(designator)}

    markers: list[Marker] = []
    other_pages = False
    for designator in sorted(drawable, key=natural_sort_key):
        kind = MarkerKind.NAMED if designator in named else MarkerKind.CLASS_ONLY
        for shape in shapes[designator]:
            if shape.page_index == page_index:
                markers.append(Marker(designator, kind, shape))
            else:
                other_pages = True

    hint = "X-ray mode: none on this page" if drawable and not markers else None

    lines: list[LegendLine] = []
    if is_stale:
        lines.append(LegendLine("Rescan pending: these are the last scan's X-ray locations"))
    lines.append(LegendLine(_plural(len(every), "X-ray location")))
    if locations.eco_table:
        lines.append(LegendLine(f"ECO X-ray table: {len(locations.eco_table):,}"))
    if locations.bom_notes:
        lines.append(LegendLine(f"BOM notes: {len(locations.bom_notes):,}"))
    lines.extend(_class_lines(locations))
    not_in_bom = len(every - view.bom_designators)
    if not_in_bom:
        lines.append(LegendLine(f"{not_in_bom:,} not in the BOM"))
    not_drawn = len(in_bom - drawable)
    if not_drawn:
        lines.append(LegendLine(f"{not_drawn:,} not on this drawing"))
    lines.extend(_unresolved_lines(locations))
    for reason in unknown:
        lines.append(LegendLine(f"X-ray requirement unknown: {reason}"))
    return MarkerLayout(tuple(markers), tuple(lines), hint, other_pages)


def cut_legend(lines: Sequence[LegendLine], fits) -> list[str]:
    """§6.5's bound: cuts lines, lowest cut_rank first and the last of a rank first, until fits(texts) is
    true, ending with "…" when anything was cut."""
    current = list(lines)
    texts = [line.text for line in current]
    if fits(texts):
        return texts
    while True:
        cuttable = [(line.cut_rank, -index) for index, line in enumerate(current) if line.cut_rank is not None]
        if not cuttable:
            return texts + ["…"]
        _rank, negative_index = min(cuttable)
        index = -negative_index
        line = current[index]
        if line.short_text is not None:
            current[index] = LegendLine(line.short_text)
        else:
            del current[index]
        texts = [line.text for line in current]
        if fits(texts + ["…"]):
            return texts + ["…"]
