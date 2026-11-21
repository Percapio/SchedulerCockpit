"""Company short names (Phase 59 §5): the key, the display rule, the stored JSON and the refusals.

Qt-free. The settings controller that owns the map lives in cockpit/ui/company_names.py. Customer
names come from travelers on a share others write to; nothing here logs them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

COMPANY_NAMES_SETTINGS_KEY: str = "audit_list/company_short_names"
COMPANY_NAMES_FORMAT: int = 1
MAX_COMPANY_NAME_ENTRIES: int = 500
MAX_TRAVELER_NAME_CHARS: int = 255
MAX_SHORT_NAME_CHARS: int = 40


@dataclass(frozen=True)
class ShortNameEntry:
    traveler_name: str
    short_name: str


CompanyNameMap = Mapping[str, ShortNameEntry]


@dataclass(frozen=True)
class BlankTravelerName:
    pass


@dataclass(frozen=True)
class TravelerNameTooLong:
    limit: int


@dataclass(frozen=True)
class ShortNameTooLong:
    limit: int


@dataclass(frozen=True)
class LimitReached:
    limit: int


ShortNameRefused = BlankTravelerName | TravelerNameTooLong | ShortNameTooLong | LimitReached


def refusal_text(refusal: ShortNameRefused) -> str:
    if isinstance(refusal, BlankTravelerName):
        return "The traveler's name is blank."
    if isinstance(refusal, TravelerNameTooLong):
        return f"Names over {refusal.limit} characters can't be given a short name."
    if isinstance(refusal, ShortNameTooLong):
        return f"The short name is over {refusal.limit} characters."
    return f"{refusal.limit} names is the limit."


def company_key(customer: str | None) -> str:
    """The key two spellings of one customer share: trimmed, spaces collapsed, casefolded."""
    return " ".join(str(customer or "").split()).casefold()


def company_display_name(customer: str | None, names: CompanyNameMap) -> str:
    """What the Company cell shows: the mapped short name, else the traveler's name trimmed."""
    trimmed = str(customer or "").strip()
    if not trimmed:
        return ""
    entry = names.get(company_key(trimmed))
    return entry.short_name if entry is not None else trimmed


def company_names_to_json(names: CompanyNameMap) -> str:
    return json.dumps({
        "format": COMPANY_NAMES_FORMAT,
        "entries": [
            {"traveler_name": entry.traveler_name, "short_name": entry.short_name}
            for entry in names.values()
        ],
    }, ensure_ascii=False, separators=(",", ":"))


def _parse_entry(raw: Any) -> ShortNameEntry | None:
    if not isinstance(raw, dict):
        return None
    traveler, short = raw.get("traveler_name"), raw.get("short_name")
    if not isinstance(traveler, str) or not isinstance(short, str):
        return None
    traveler, short = traveler.strip(), short.strip()
    if not traveler or not short:
        return None
    if len(traveler) > MAX_TRAVELER_NAME_CHARS or len(short) > MAX_SHORT_NAME_CHARS:
        return None
    return ShortNameEntry(traveler, short)


def parse_company_names(raw: Any) -> dict[str, ShortNameEntry] | None:
    """The stored map, or None when the value doesn't parse or names a newer format (§5.2).

    An entry with a field that isn't text, a blank name or a name over its limit is dropped alone;
    of entries sharing a key the first is kept; past MAX_COMPANY_NAME_ENTRIES keys the rest go.
    """
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
    if type(stored_format) is not int or not 1 <= stored_format <= COMPANY_NAMES_FORMAT:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    names: dict[str, ShortNameEntry] = {}
    for raw_entry in entries:
        if len(names) >= MAX_COMPANY_NAME_ENTRIES:
            break
        entry = _parse_entry(raw_entry)
        if entry is None:
            continue
        names.setdefault(company_key(entry.traveler_name), entry)
    return names
