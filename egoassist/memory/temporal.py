"""Authoritative wall-clock context and small deterministic time semantics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.10+ always provides zoneinfo
    ZoneInfo = None  # type: ignore[assignment]


_DATE_ISO = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_DATE_CJK = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")
_CLOCK = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{1,2})|点(?:(\d{1,2})分?)?)(?!\d)")
_CJK_CLOCK = re.compile(r"([零〇一二两三四五六七八九十]{1,3})点(?:([零〇一二两三四五六七八九十]{1,3})分?)?")


def _fixed_offset(minutes: int) -> timezone:
    bounded = max(-14 * 60, min(14 * 60, int(minutes)))
    return timezone(timedelta(minutes=bounded))


def _timezone(name: str, offset_minutes: int):
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return _fixed_offset(offset_minutes)


@dataclass(frozen=True)
class ClockContext:
    """Server-authoritative instant interpreted in the participant's civil time."""

    now_utc: datetime
    timezone_name: str
    utc_offset_minutes: int

    @classmethod
    def current(
        cls,
        timezone_name: str = "UTC",
        utc_offset_minutes: int = 0,
        *,
        now_utc: datetime | None = None,
    ) -> "ClockContext":
        now = now_utc or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        tz = _timezone(str(timezone_name or "UTC"), int(utc_offset_minutes))
        actual_offset = int((now.astimezone(tz).utcoffset() or timedelta()).total_seconds() // 60)
        return cls(now, str(timezone_name or "UTC"), actual_offset)

    @property
    def tzinfo(self):
        return _timezone(self.timezone_name, self.utc_offset_minutes)

    @property
    def local_now(self) -> datetime:
        return self.now_utc.astimezone(self.tzinfo)

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "nowUtc": self.now_utc.isoformat(timespec="seconds"),
            "localNow": self.local_now.isoformat(timespec="seconds"),
            "timezone": self.timezone_name,
            "utcOffsetMinutes": self.utc_offset_minutes,
            "authority": "server-clock",
            "timezoneSource": "iana" if getattr(self.tzinfo, "key", None) else "fixed-offset-fallback",
        }


def _cjk_number(value: str) -> int | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if all(char in digits for char in value):
        return int("".join(str(digits[char]) for char in value))
    return None


def _clock_from_text(text: str) -> tuple[int, int] | None:
    match = _CLOCK.search(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or match.group(3) or 0)
    else:
        cjk = _CJK_CLOCK.search(text)
        if not cjk:
            lowered = text.casefold()
            if any(marker in lowered for marker in ("中午", "noon")):
                return 12, 0
            if any(marker in lowered for marker in ("午夜", "midnight")):
                return 0, 0
            return None
        hour = _cjk_number(cjk.group(1))
        minute = _cjk_number(cjk.group(2)) if cjk.group(2) else 0
        if hour is None or minute is None:
            return None
    lowered = text.casefold()
    if any(marker in lowered for marker in ("下午", "晚上", "傍晚", "pm")) and hour < 12:
        hour += 12
    if any(marker in lowered for marker in ("凌晨", "上午", "早上", "am")) and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def _calendar_date(text: str, clock: ClockContext) -> tuple[date, bool]:
    local = clock.local_now
    lowered = text.casefold()
    iso = _DATE_ISO.search(text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))), True
        except ValueError:
            return local.date(), False
    cjk = _DATE_CJK.search(text)
    if cjk:
        try:
            return date(int(cjk.group(1) or local.year), int(cjk.group(2)), int(cjk.group(3))), True
        except ValueError:
            return local.date(), False
    if any(marker in lowered for marker in ("前天", "day before yesterday")):
        return local.date() - timedelta(days=2), True
    if any(marker in lowered for marker in ("昨天", "昨日", "yesterday")):
        return local.date() - timedelta(days=1), True
    if any(marker in lowered for marker in ("后天", "day after tomorrow")):
        return local.date() + timedelta(days=2), True
    if any(marker in lowered for marker in ("明天", "tomorrow")):
        return local.date() + timedelta(days=1), True
    if any(marker in lowered for marker in ("今天", "今日", "today", "tonight")):
        return local.date(), True
    return local.date(), False


def _local_datetime(day: date, clock_value: tuple[int, int], clock: ClockContext) -> datetime:
    """Create a civil instant and normalize nonexistent DST wall times forward."""

    naive = datetime.combine(day, time(*clock_value))
    local = naive.replace(tzinfo=clock.tzinfo)
    round_trip = local.astimezone(timezone.utc).astimezone(clock.tzinfo)
    if round_trip.replace(tzinfo=None) != naive:
        return round_trip
    return local


def resolve_time_semantics(text: str, clock: ClockContext, *, kind: str | None) -> dict[str, Any]:
    """Resolve common civil-time expressions without trusting model-generated timestamps."""

    value = " ".join(str(text).split())
    lowered = value.casefold()
    target_date, date_explicit = _calendar_date(value, clock)
    clock_value = _clock_from_text(value)
    # Multiple dates describe potentially different events. Do not silently
    # choose whichever keyword happens to be checked first.
    mentions = set(re.findall(
        r"day before yesterday|day after tomorrow|yesterday|tomorrow|today|tonight|"
        r"前天|昨天|昨日|今天|今日|明天|后天|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}日",
        lowered,
    ))
    ambiguous = len(mentions) > 1
    recurrence = None
    if any(marker in lowered for marker in ("每天", "每日", "every day", "daily")):
        recurrence = {
            "frequency": "daily",
            "localTime": f"{clock_value[0]:02d}:{clock_value[1]:02d}" if clock_value else None,
            "timezone": clock.timezone_name,
        }
    elif any(marker in lowered for marker in ("每周", "每星期", "every week", "weekly")):
        recurrence = {
            "frequency": "weekly",
            "localTime": f"{clock_value[0]:02d}:{clock_value[1]:02d}" if clock_value else None,
            "timezone": clock.timezone_name,
        }

    effective_at = None
    effective_end = None
    if ambiguous:
        pass
    elif date_explicit and clock_value is None:
        local_start = _local_datetime(target_date, (0, 0), clock)
        local_end = _local_datetime(target_date + timedelta(days=1), (0, 0), clock)
        effective_at = local_start.astimezone(timezone.utc)
        effective_end = local_end.astimezone(timezone.utc)
    elif date_explicit or clock_value is not None:
        hour, minute = clock_value or (clock.local_now.hour, clock.local_now.minute)
        local_point = _local_datetime(target_date, (hour, minute), clock)
        effective_at = local_point.astimezone(timezone.utc)
    elif kind in {"event", "state", "observation", "scene", "tool-effect"}:
        effective_at = clock.now_utc

    return {
        "recordedAt": clock.now_utc.isoformat(timespec="milliseconds"),
        "effectiveAt": effective_at.isoformat(timespec="milliseconds") if effective_at else None,
        "effectiveEnd": effective_end.isoformat(timespec="milliseconds") if effective_end else None,
        "timezone": clock.timezone_name,
        "recurrence": recurrence,
        "dateExplicit": date_explicit,
        "clockExplicit": clock_value is not None,
        "ambiguous": ambiguous,
    }


def query_time_range(text: str, clock: ClockContext) -> tuple[datetime | None, datetime | None]:
    semantics = resolve_time_semantics(text, clock, kind=None)
    start = semantics["effectiveAt"]
    end = semantics["effectiveEnd"]
    if not semantics["dateExplicit"] and not semantics["clockExplicit"]:
        return None, None
    if start and not end and semantics["clockExplicit"]:
        end = (datetime.fromisoformat(start) + timedelta(minutes=1)).isoformat()
    return (
        datetime.fromisoformat(start) if start else None,
        datetime.fromisoformat(end) if end else None,
    )
