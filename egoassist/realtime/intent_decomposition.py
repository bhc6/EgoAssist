"""Typed, conservative decomposition of one transcript into executable intents."""

from __future__ import annotations

import re
from typing import Any, Mapping

from egoassist.product.contracts import IntentCandidate


_ENGLISH_DELAY = re.compile(
    r"\b(?:in|after)\s+(\d{1,6})\s*(seconds?|minutes?|hours?)\b",
    re.IGNORECASE,
)
_CHINESE_DELAY = re.compile(r"(\d{1,6})\s*(秒(?:钟)?|分钟|小时)\s*(?:后|以后)")


def _delay_seconds(text: str) -> int | None:
    english = _ENGLISH_DELAY.search(text)
    if english:
        value = int(english.group(1))
        unit = english.group(2).casefold()
        return value * (3600 if unit.startswith("hour") else 60 if unit.startswith("minute") else 1)
    chinese = _CHINESE_DELAY.search(text)
    if chinese:
        value = int(chinese.group(1))
        unit = chinese.group(2)
        return value * (3600 if unit == "小时" else 60 if unit == "分钟" else 1)
    return None


def _reminder_label(text: str) -> str:
    value = str(text).strip()
    value = _ENGLISH_DELAY.sub("", value)
    value = _CHINESE_DELAY.sub("", value)
    value = re.sub(
        r"^(?:please\s+)?(?:remind me(?:\s+to)?|set (?:a )?(?:timer|reminder)(?:\s+to)?)\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"^(?:请)?(?:提醒我|设置(?:一个)?(?:计时器|提醒))(?:去|要)?", "", value)
    return value.strip(" ,，。;；")[:80] or "Reminder"


def normalize_intent_candidates(
    transcript: str,
    semantic: Mapping[str, Any],
) -> list[IntentCandidate]:
    raw = semantic.get("intents")
    candidates: list[IntentCandidate] = []
    if isinstance(raw, list):
        for item in raw:
            candidates.append(IntentCandidate.model_validate(item))
    if not candidates:
        candidates = [IntentCandidate(
            kind=str(semantic.get("intentKind") or "foreground-dialogue"),
            text=str(transcript).strip(),
            parameters={},
        )]

    normalized: list[IntentCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        parameters = dict(candidate.parameters)
        if candidate.kind == "background-reminder":
            delay = parameters.get("delaySeconds")
            try:
                delay = int(delay) if delay is not None else _delay_seconds(candidate.text)
            except (TypeError, ValueError):
                delay = None
            if delay is not None and 1 <= delay <= 31_536_000:
                parameters["delaySeconds"] = delay
            else:
                parameters.pop("delaySeconds", None)
            parameters["label"] = str(
                parameters.get("label") or _reminder_label(candidate.text)
            ).strip()[:80] or "Reminder"
        key = (candidate.kind, candidate.text.casefold())
        if key in seen:
            continue
        seen.add(key)
        normalized.append(candidate.model_copy(update={"parameters": parameters}))
    return normalized
