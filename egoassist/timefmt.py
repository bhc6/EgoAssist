"""Shared 北京时间 (UTC+8) display formatting.

Previously three byte-identical/near-identical `_fmt_beijing` helpers lived in
qa/pipeline.py, qa/timeline_pipeline.py and run_egoassist.py with two subtly
different signatures (datetime vs ISO string).  They are unified here:

  * ``format_beijing``     — canonical formatter, takes a ``datetime``.
  * ``format_beijing_iso`` — parses an ISO-8601 UTC string, then delegates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

_BEIJING = timezone(timedelta(hours=8))


def format_beijing(dt: Optional[datetime]) -> Optional[str]:
    """Format a datetime as a 北京时间 display string; ``None`` passes through."""
    if dt is None:
        return None
    return dt.astimezone(_BEIJING).strftime("北京时间 %Y-%m-%d %H:%M:%S")


def format_beijing_iso(iso_str: Optional[str]) -> Optional[str]:
    """Parse an ISO-8601 UTC string then format it; invalid input → ``None``."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return format_beijing(dt)
