# timeutils.py
"""Central time helpers.

`datetime.utcnow()` is deprecated (and slated for removal), so all UTC "now"
reads go through utcnow() here. It returns a tz-NAIVE UTC datetime so it stays
comparable with the existing naive DateTime columns already stored in SQLite —
swapping to tz-aware values would break subtraction against those rows.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a tz-naive datetime (drop-in for datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
