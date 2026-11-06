"""Version tags read from document file names.

Phase 55 section 3.2. A revision is recognised by the date in its name, not by
a word: one sample's tags carry no "update" at all ("ECO_10-30-26").
"""

import datetime
import re
from dataclasses import dataclass
from typing import Final, Union

# The shapes accepted, not an order to try: the first group's length alone
# decides how a candidate is read, so no two shapes can disagree.
VERSION_DATE_FORMATS: Final[tuple[str, ...]] = (
    "M-D-YYYY", "M-D-YY", "M.D.YYYY", "M.D.YY", "YYYY-M-D", "YYYY.M.D",
)

# Zero-width so candidates may overlap: "1-2-3-2026" must still offer
# "2-3-2026" after "1-2-3" is refused. The look-arounds are what keep a job
# number such as B141837 from ever reading as a date.
_DATE_CANDIDATE: Final = re.compile(
    r"(?=(?<!\d)(?P<first>\d{1,4})(?P<sep>[-.])(?P<second>\d{1,2})(?P=sep)(?P<third>\d{1,4})(?!\d))"
)


@dataclass(frozen=True, order=True)
class Dated:
    date: datetime.date


@dataclass(frozen=True)
class Undated:
    pass


UNDATED: Final[Undated] = Undated()

VersionTag = Union[Dated, Undated]


def _reading(first: str, second: str, third: str) -> datetime.date | None:
    """The one date a candidate can mean, or None.

    post: a four-digit first group reads year-month-day; a one- or two-digit
          first group reads month-day-year with a two- or four-digit year, two
          digits meaning 20YY; anything else, and any impossible calendar date,
          is None
    """
    if len(first) == 4:
        if len(third) > 2:
            return None
        year, month, day = int(first), int(second), int(third)
    elif len(first) <= 2:
        if len(third) == 2:
            year = 2000 + int(third)
        elif len(third) == 4:
            year = int(third)
        else:
            return None
        month, day = int(first), int(second)
    else:
        return None
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def version_tag(file_name: str) -> VersionTag:
    """Reads the version tag a revision carries in its file name.

    post: Dated iff the name holds a complete date in one of
          VERSION_DATE_FORMATS, anywhere in the name, with or without a word
          around it; when the name holds several, the latest counts; partial
          dates ("Updated 5-21", "update 11-2026") and revision numbers
          ("rev 1.2") are Undated
    """
    dates = [
        reading
        for match in _DATE_CANDIDATE.finditer(file_name)
        if (reading := _reading(match["first"], match["second"], match["third"])) is not None
    ]
    if not dates:
        return UNDATED
    return Dated(max(dates))


def tag_rank(tag: VersionTag) -> tuple[int, datetime.date]:
    """Orders tags: Undated below every Dated, Dated tags by date."""
    if isinstance(tag, Dated):
        return (1, tag.date)
    return (0, datetime.date.min)
