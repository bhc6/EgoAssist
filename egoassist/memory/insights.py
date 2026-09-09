"""One evidence-to-report path for calendar summaries and optional suggestions."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time as day_time, timedelta, timezone
import hashlib
import json
import os
import time
from pathlib import Path

import aiohttp

from egoassist.prompts import prompt_text
from .temporal import ClockContext


def calendar_period(kind: str, anchor: str | None, clock: ClockContext) -> dict:
    if kind not in {"daily", "weekly", "monthly", "suggestions"}:
        raise ValueError("unsupported insight period")
    day = date.fromisoformat(anchor) if anchor else clock.local_now.date()
    if kind == "weekly":
        first = day - timedelta(days=day.weekday())
        last = first + timedelta(days=7)
    elif kind == "monthly":
        first = day.replace(day=1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        first, last = day, day + timedelta(days=1)
    return {"kind": kind, "anchor": day.isoformat(), "timezone": clock.timezone_name,
            "start": datetime.combine(first, day_time.min, clock.tzinfo).isoformat(),
            "end": datetime.combine(last, day_time.min, clock.tzinfo).isoformat(),
            "basis": "recorded-time", "endExclusive": True}


def collect_sources(paths: list[Path], period: dict, *, limit: int = 300, char_budget: int = 32000) -> dict:
    lower, upper = datetime.fromisoformat(period["start"]), datetime.fromisoformat(period["end"])
    records, errors = [], []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append({"sessionId": path.stem, "error": type(exc).__name__})
            continue
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    raise ValueError("journal entry must be an object")
                if entry.get("type") != "message" or entry.get("status") != "completed":
                    continue
                message = entry.get("message") or {}
                if message.get("role") != "user":
                    continue
                stamp = datetime.fromisoformat(entry["timestamp"])
                if stamp.tzinfo is None:
                    raise ValueError("missing record timezone")
                if lower <= stamp < upper:
                    records.append({"sourceId": path.stem + ":" + entry["id"], "sessionId": path.stem,
                                    "eventId": entry["id"], "recordedAt": entry["timestamp"],
                                    "text": message["content"]})
            except (ValueError, TypeError, KeyError) as exc:
                errors.append({"sessionId": path.stem, "line": number, "error": type(exc).__name__})
    records.sort(key=lambda item: (item["recordedAt"], item["sourceId"]))
    selected, size = [], 0
    for item in reversed(records):
        cost = len(json.dumps(item, ensure_ascii=False))
        if len(selected) >= limit or size + cost > char_budget:
            continue
        selected.append(item)
        size += cost
    selected.reverse()
    return {"records": selected, "totalRecords": len(records), "includedRecords": len(selected),
            "omittedRecords": len(records) - len(selected), "errors": errors,
            "complete": len(selected) == len(records) and not errors}


def validate_insights(raw: str, sources: list[dict]) -> dict:
    value = json.loads(raw)
    allowed = {item["sourceId"] for item in sources}
    if not isinstance(value, dict) or set(value) != {"summary", "suggestions"}:
        raise ValueError("invalid insight envelope")
    for key, limit in (("summary", 12), ("suggestions", 3)):
        items = value[key]
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("invalid insight item count")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "sourceIds"}:
                raise ValueError("invalid insight item")
            if not isinstance(item["text"], str) or not item["text"].strip() or len(item["text"]) > 2000:
                raise ValueError("invalid insight text")
            refs = item["sourceIds"]
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
                raise ValueError("insight cites missing source")
    return value


class InsightGenerator:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.model = os.getenv("EGOASSIST_INSIGHT_MODEL", "qwen-flash")
        self.timeout = float(os.getenv("EGOASSIST_INSIGHT_TIMEOUT", "30"))
        self._locks: dict[str, asyncio.Lock] = {}

    async def generate(self, *, user_id: str, journal, paths: list[Path], kind: str,
                       anchor: str | None, clock: ClockContext) -> dict:
        # Concurrent UI refreshes share the persisted result for identical input.
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            return {"status": "busy", "kind": kind}
        async with lock:
            return await self._generate(journal, paths, kind, anchor, clock)

    async def _generate(self, journal, paths, kind, anchor, clock):
        period = calendar_period(kind, anchor, clock)
        started = time.perf_counter()
        sources = await asyncio.to_thread(collect_sources, paths, period)
        policy = prompt_text("insights/generate")
        data = {"period": period, "coverage": {k: v for k, v in sources.items() if k != "records"},
                "sources": sources["records"]}
        fingerprint = hashlib.sha256(json.dumps([self.model, policy, data], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        previous = next((row for row in reversed(journal.entries()) if row.get("type") == "insight.result"
                         and row.get("fingerprint") == fingerprint and row.get("status") in {"completed", "empty"}), None)
        if previous:
            return {**previous, "cached": True}
        messages = [{"role": "system", "content": policy},
                    {"role": "user", "content": json.dumps({**data, "clock": clock.prompt_payload()}, ensure_ascii=False)}]
        request = journal.append("insight.request", {"fingerprint": fingerprint, "model": self.model,
                                 "period": period, "messages": messages, "coverage": data["coverage"]})
        result = {"fingerprint": fingerprint, "requestEventId": request["id"], "period": period,
                  "model": self.model, "coverage": data["coverage"], "sources": sources["records"],
                  "summary": [], "suggestions": [], "cached": False, "calibratedProbability": None}
        try:
            if not sources["records"]:
                result["status"] = "empty"
            else:
                if not self.api_key:
                    raise ValueError("QWEN_API_KEY is not configured")
                import dashscope
                url = dashscope.base_http_api_url.rstrip("/") + "/services/aigc/text-generation/generation"
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=True) as session:
                    async with session.post(url, headers={"Authorization": "Bearer " + self.api_key}, json={
                        "model": self.model, "input": {"messages": messages},
                        "parameters": {"result_format": "message", "temperature": 0, "enable_thinking": False,
                                       "max_tokens": 2400, "response_format": {"type": "json_object"}},
                    }) as response:
                        if response.status != 200:
                            raise RuntimeError(f"insight provider HTTP {response.status}")
                        payload = await response.json()
                result["rawOutput"] = payload["output"]["choices"][0]["message"]["content"]
                result["providerRequestId"] = payload.get("request_id")
                result["usage"] = payload.get("usage", {})
                result.update(validate_insights(result["rawOutput"], sources["records"]))
                result["status"] = "completed"
        except (ValueError, TypeError, KeyError, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            result.update(status="failed", error=type(exc).__name__ + ": " + str(exc)[:200],
                          timedOut=isinstance(exc, asyncio.TimeoutError))
        result["latencyMs"] = round((time.perf_counter() - started) * 1000, 3)
        return journal.append("insight.result", result, parent_id=request["id"])
