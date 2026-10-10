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
# A CJK clock reading: a whole numeral (never a suffix of a longer numeral, so
# "十一点" can't be read as "一点") followed by 点 and an optional minute.
_CJK_CLOCK_RAW = re.compile(
    r"(?<![零〇一二两三四五六七八九十])"
    r"([零〇一二两三四五六七八九十]{1,3})点"
    r"(?:([零〇一二两三四五六七八九十]{1,3})分?|(半))?"
)
# "一点" is also a degree word (多一点 / 快一点 / 有一点 / 一点儿 / 一点点).
# Only a bare 一 can be that word, so only it gets the extra guard.
_DEGREE_BEFORE = set("多快慢有好差少稍大小高低再轻重近远")
_DEGREE_AFTER = set("儿点些")


def _cjk_clock_matches(text: str) -> list[re.Match]:
    matches = []
    for match in _CJK_CLOCK_RAW.finditer(text):
        if match.group(1) == "一":
            before = text[match.start() - 1] if match.start() > 0 else ""
            after = text[match.end(1) + 1] if match.end(1) + 1 < len(text) else ""
            if before in _DEGREE_BEFORE or after in _DEGREE_AFTER:
                continue
        matches.append(match)
    return matches


class _CjkClock:
    """Regex-like facade so existing call sites keep ``search``/``findall``."""

    @staticmethod
    def search(text: str):
        found = _cjk_clock_matches(text)
        return found[0] if found else None

    @staticmethod
    def findall(text: str):
        return _cjk_clock_matches(text)


_CJK_CLOCK = _CjkClock()
_DAY_OFFSETS = {
    "大前天": -3, "前天": -2, "昨天": -1, "昨日": -1, "昨晚": -1,
    "今天": 0, "今日": 0, "今晚": 0, "今早": 0, "今晨": 0, "今中午": 0,
    "明天": 1, "明日": 1, "明早": 1, "明晚": 1, "后天": 2, "大后天": 3,
    "day before yesterday": -2, "yesterday": -1, "last night": -1,
    "today": 0, "tonight": 0, "this morning": 0, "this evening": 0,
    "tomorrow": 1, "day after tomorrow": 2,
}
_DAY_WORDS = re.compile("|".join(
    (r"\b" + re.escape(word) + r"\b") if word.isascii() else re.escape(word)
    for word in sorted(_DAY_OFFSETS, key=len, reverse=True)
), re.I)
_RELATIVE_DAYS = re.compile(r"(\d+|[一二两三四五六七八九十]+)天([前后])|\b(\d+) days? (ago|later)\b", re.I)
# 刚才 / 刚刚 / just now are not vague: they place the statement at (about) its
# own source time. Treating them as unresolved erased the effective time and
# also disabled supersede, so "钥匙刚才放桌上了" could never replace an older
# location. They are handled by the source-clock branch below instead.
_VAGUE_TIME = re.compile(r"前几天|几天前|那天|当天|当时|之前|最近|上次|下次|过几天|"
                         r"[上下本这]周|[上下这]个?月|去年|明年|星期[一二三四五六日天]|"
                         r"\b(?:recently|earlier|that day|last time|a few days ago|"
                         r"(?:last|next|this) (?:week|month|year)|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def date_mentions(text: str, clock: ClockContext) -> list[tuple[str, date | None]]:
    """Resolve each expression independently; invalid dates remain explicitly unknown."""
    result = []
    for pattern in (_DATE_ISO, _DATE_CJK):
        for match in pattern.finditer(text):
            try:
                day = date(int(match[1] or clock.local_now.year), int(match[2]), int(match[3]))
            except ValueError:
                day = None
            result.append((match[0], day))
    for match in _DAY_WORDS.finditer(text):
        result.append((match[0], clock.local_now.date() + timedelta(days=_DAY_OFFSETS[match[0].casefold()])))
    for match in _RELATIVE_DAYS.finditer(text):
        count = (int(match[1]) if match[1].isdigit() else _cjk_number(match[1])) if match[1] else int(match[3])
        offset = -count if match[2] == "前" or (match[4] or "").casefold() == "ago" else count
        try:
            day = clock.local_now.date() + timedelta(days=offset)
        except OverflowError:
            day = None
        result.append((match[0], day))
    return result


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
            "nowUtc": self.now_utc.isoformat(timespec="milliseconds"),
            "localNow": self.local_now.isoformat(timespec="milliseconds"),
            "timezone": self.timezone_name,
            "utcOffsetMinutes": self.utc_offset_minutes,
            "authority": "server-clock",
            "timezoneSource": "iana" if getattr(self.tzinfo, "key", None) else "fixed-offset-fallback",
        }

    def at_source(self, timestamp: str | None) -> "ClockContext":
        """Retain civil timezone while anchoring delayed work to its durable input."""
        if not timestamp:
            return self
        return self.current(self.timezone_name, self.utc_offset_minutes,
            now_utc=datetime.fromisoformat(timestamp.replace("Z", "+00:00")))


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
        minute = 30 if cjk.group(3) else (_cjk_number(cjk.group(2)) if cjk.group(2) else 0)
        if hour is None or minute is None:
            return None
    lowered = text.casefold()
    # "晚八点" / "明晚十一点": 晚 directly before the number is an evening marker.
    evening = bool(re.search(r"晚(?=[零〇一二两三四五六七八九十\d])", text))
    if (evening or any(marker in lowered for marker in ("下午", "晚上", "傍晚")) or re.search(r"\bpm\b", lowered)) and hour < 12:
        hour += 12
    if (any(marker in lowered for marker in ("凌晨", "上午", "早上")) or re.search(r"\bam\b", lowered)) and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


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
    mentions = date_mentions(value, clock)
    target_date = next((day for _, day in mentions if day is not None), clock.local_now.date())
    date_explicit = bool(mentions)
    clock_value = _clock_from_text(value)
    # Multiple dates describe potentially different events. Do not silently
    # choose whichever keyword happens to be checked first.
    invalid = any(day is None for _, day in mentions) or bool((_CLOCK.search(value) or _CJK_CLOCK.search(value)) and clock_value is None)
    unresolved = bool(_VAGUE_TIME.search(value))
    ambiguous = len({day for _, day in mentions}) > 1 or len(_CLOCK.findall(value)) + len(_CJK_CLOCK.findall(value)) > 1
    # Quoted relative dates can be anchored to another speaker's turn.
    ambiguous = ambiguous or bool(re.search(r'[“「『"].*(?:今天|昨天|明天|\btoday\b|\byesterday\b|\btomorrow\b)', value, re.I))
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
    if ambiguous or invalid or unresolved or recurrence:
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
    # A reported occurrence without a date need not have happened when spoken.
    # Observations and current states retain their source-clock semantics;
    # callers with a measured event timestamp can pass it explicitly.
    elif kind in {"state", "observation", "scene", "tool-effect"}:
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
        "unresolved": unresolved or invalid,
        "status": "needs-clarification" if ambiguous or unresolved or invalid else "resolved" if effective_at else "unspecified",
        "expressions": [{"expression": phrase, "localDate": day.isoformat() if day else None} for phrase, day in mentions],
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
