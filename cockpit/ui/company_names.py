"""The company short-name map's owner (Phase 59 §5.2): read once, written on every change."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from PyQt6.QtCore import QObject, pyqtSignal

from cockpit.services.company_names import (
    COMPANY_NAMES_SETTINGS_KEY,
    MAX_COMPANY_NAME_ENTRIES,
    MAX_SHORT_NAME_CHARS,
    MAX_TRAVELER_NAME_CHARS,
    BlankTravelerName,
    LimitReached,
    ShortNameEntry,
    ShortNameRefused,
    ShortNameTooLong,
    TravelerNameTooLong,
    company_key,
    company_names_to_json,
    parse_company_names,
)
from cockpit.services.requirements import Failure, Result, Success

logger = logging.getLogger(__name__)


class CompanyNameController(QObject):
    """Owns the short-name map kept in QSettings (§5.2)."""

    changed = pyqtSignal()

    def __init__(self, settings: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._names: dict[str, ShortNameEntry] = {}
        raw = settings.value(COMPANY_NAMES_SETTINGS_KEY) if settings is not None else None
        if raw is not None:
            parsed = parse_company_names(raw)
            if parsed is None:
                logger.warning("%s couldn't be read; no short names until the next change",
                               COMPANY_NAMES_SETTINGS_KEY)
            else:
                self._names = parsed

    def names(self) -> dict[str, ShortNameEntry]:
        return dict(self._names)

    def set_short_name(self, traveler_name: str, short_name: str) -> Result[dict, ShortNameRefused]:
        """Maps one traveler customer name to a short display name, or removes the mapping.

        Checked in §5.2's order; nothing is written on a Failure, and changed is emitted iff the
        stored map changed.
        """
        traveler = str(traveler_name or "").strip()
        short = str(short_name or "").strip()
        if not traveler:
            return Failure(BlankTravelerName())
        key = company_key(traveler)
        if not short:
            if key in self._names:
                del self._names[key]
                self._store()
            return Success(self.names())
        if len(traveler) > MAX_TRAVELER_NAME_CHARS:
            return Failure(TravelerNameTooLong(MAX_TRAVELER_NAME_CHARS))
        if len(short) > MAX_SHORT_NAME_CHARS:
            return Failure(ShortNameTooLong(MAX_SHORT_NAME_CHARS))
        existing = self._names.get(key)
        if existing is None and len(self._names) >= MAX_COMPANY_NAME_ENTRIES:
            return Failure(LimitReached(MAX_COMPANY_NAME_ENTRIES))
        if existing is not None and existing.short_name == short:
            return Success(self.names())
        spelling = existing.traveler_name if existing is not None else traveler
        self._names[key] = ShortNameEntry(spelling, short)
        self._store()
        return Success(self.names())

    def remove_unused(self, open_names: Iterable[str]) -> int:
        """Removes the entries whose key no open audit's customer name has; returns how many went."""
        keep = {company_key(name) for name in open_names}
        doomed = [key for key in self._names if key not in keep]
        for key in doomed:
            del self._names[key]
        if doomed:
            self._store()
        return len(doomed)

    def _store(self) -> None:
        if self._settings is not None:
            self._settings.setValue(COMPANY_NAMES_SETTINGS_KEY, company_names_to_json(self._names))
        self.changed.emit()
