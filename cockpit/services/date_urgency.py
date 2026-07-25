from datetime import date, timedelta
from enum import Enum
from typing import Set


class DateUrgency(Enum):
    OVERDUE = "overdue"
    DUE_SOON = "due_soon"
    COMFORTABLE = "comfortable"


_DUE_SOON_WINDOW_WORKING_DAYS: int = 3


def _add_working_days(start: date, count: int, holidays: Set[date]) -> date:
    cursor: date = start
    remaining: int = count
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5 and cursor not in holidays:
            remaining -= 1
    return cursor


def classify_urgency(target: date | None, today: date, holidays: Set[date]) -> DateUrgency:
    if target is None:
        return DateUrgency.COMFORTABLE
    if target < today:
        return DateUrgency.OVERDUE
    due_soon_cutoff: date = _add_working_days(today, _DUE_SOON_WINDOW_WORKING_DAYS, holidays)
    return DateUrgency.DUE_SOON if target <= due_soon_cutoff else DateUrgency.COMFORTABLE
