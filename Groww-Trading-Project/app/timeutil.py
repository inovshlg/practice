from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def now_naive() -> datetime:
    """Current IST wall-clock time without tzinfo (matches candle timestamps)."""
    return now_ist().replace(tzinfo=None)
