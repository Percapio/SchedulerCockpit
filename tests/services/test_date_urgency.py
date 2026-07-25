from datetime import date
from cockpit.services.date_urgency import DateUrgency, classify_urgency


def test_classify_urgency_none():
    today = date(2026, 7, 23)
    assert classify_urgency(None, today, set()) == DateUrgency.COMFORTABLE


def test_classify_urgency_overdue():
    today = date(2026, 7, 23)
    yesterday = date(2026, 7, 22)
    last_week = date(2026, 7, 15)
    assert classify_urgency(yesterday, today, set()) == DateUrgency.OVERDUE
    assert classify_urgency(last_week, today, set()) == DateUrgency.OVERDUE


def test_classify_urgency_due_soon_and_comfortable():
    # Thursday, July 23, 2026
    today = date(2026, 7, 23)
    
    # Today is DUE_SOON
    assert classify_urgency(today, today, set()) == DateUrgency.DUE_SOON
    
    # Friday July 24 is 1 working day -> DUE_SOON
    assert classify_urgency(date(2026, 7, 24), today, set()) == DateUrgency.DUE_SOON
    
    # Sat/Sun July 25/26 are weekend -> DUE_SOON (<= 3 working days cutoff)
    assert classify_urgency(date(2026, 7, 25), today, set()) == DateUrgency.DUE_SOON
    assert classify_urgency(date(2026, 7, 26), today, set()) == DateUrgency.DUE_SOON
    
    # Monday July 27 is 2 working days -> DUE_SOON
    assert classify_urgency(date(2026, 7, 27), today, set()) == DateUrgency.DUE_SOON
    
    # Tuesday July 28 is 3 working days (exact cutoff) -> DUE_SOON
    assert classify_urgency(date(2026, 7, 28), today, set()) == DateUrgency.DUE_SOON
    
    # Wednesday July 29 is 4 working days -> COMFORTABLE
    assert classify_urgency(date(2026, 7, 29), today, set()) == DateUrgency.COMFORTABLE


def test_classify_urgency_with_holidays():
    # Thursday, July 23, 2026
    today = date(2026, 7, 23)
    
    # Suppose Monday July 27 is a holiday
    holidays = {date(2026, 7, 27)}
    
    # Now: Fri Jul 24 (day 1), Tue Jul 28 (day 2), Wed Jul 29 (day 3).
    # So Wed Jul 29 is now DUE_SOON!
    assert classify_urgency(date(2026, 7, 29), today, holidays) == DateUrgency.DUE_SOON
    
    # Thu Jul 30 is day 4 -> COMFORTABLE
    assert classify_urgency(date(2026, 7, 30), today, holidays) == DateUrgency.COMFORTABLE
