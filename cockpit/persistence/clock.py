"""UTC clock shim. Single source of truth for current time."""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current time in UTC as a timezone-aware datetime."""
    return datetime.now(timezone.utc)
