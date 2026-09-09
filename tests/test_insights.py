import asyncio
from datetime import datetime, timezone
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.memory.insights import calendar_period, collect_sources, validate_insights
from egoassist.memory.temporal import ClockContext
from egoassist.realtime.context_harness import SessionJournal
from run_egoassist import create_app


def test_report_calendar_uses_local_days_week_and_month():
    clock = ClockContext.current("America/New_York", now_utc=datetime(2024, 3, 10, 15, tzinfo=timezone.utc))
    daily = calendar_period("daily", None, clock)
    duration = datetime.fromisoformat(daily["end"]) - datetime.fromisoformat(daily["start"])
    assert duration.total_seconds() == 23 * 3600
    week = calendar_period("weekly", "2024-03-10", clock)
    assert week["start"].startswith("2024-03-04")
    assert week["end"].startswith("2024-03-11")
    month = calendar_period("monthly", "2024-02-29", clock)
    assert month["start"].startswith("2024-02-01")
    assert month["end"].startswith("2024-03-01")


def test_report_keeps_original_user_evidence_and_discloses_budget(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    for role in ("user", "assistant", "user"):
        journal.append_message(role, "original source", source="test")
    period = calendar_period("daily", None, ClockContext.current("UTC"))
    selected = collect_sources([journal.path], period, limit=1)
    assert selected["totalRecords"] == 2
    assert selected["includedRecords"] == 1
    assert selected["complete"] is False
    assert selected["records"][0]["text"] == "original source"


def test_report_rejects_fabricated_citations_and_confidence():
    item = {"text": "summary", "sourceIds": ["not-provided"]}
    with pytest.raises(ValueError, match="missing source"):
        validate_insights(json.dumps({"summary": [item], "suggestions": []}), [])
    with pytest.raises(ValueError, match="envelope"):
        validate_insights('{"summary":[],"suggestions":[],"confidence":0.99}', [])


def test_insight_endpoints_enforce_owner_and_auto_policy(tmp_path):
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "runtime.json"), "unused", "Ethan")
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/auth/register", json={"username": "owner", "password": "owner-password-123"})
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            response = await client.post("/api/insights", json={"sessionId": sid, "kind": "daily"})
            result = await response.json()
            assert result["status"] == "empty"
            assert result["coverage"]["complete"] is True
            again = await (await client.post("/api/insights", json={"sessionId": sid, "kind": "daily"})).json()
            assert again["cached"] is True
            response = await client.post("/api/insights", json={"sessionId": sid, "kind": "suggestions", "automatic": True})
            assert response.status == 400
            await client.post("/api/auth/logout")
            await client.post("/api/auth/register", json={"username": "other", "password": "other-password-123"})
            for route in ("/api/insights", "/api/prompts"):
                response = await client.get(route, params={"session": sid})
                assert response.status in {400, 403, 404}
    asyncio.run(run())


def test_account_deletion_cancels_report_before_data_can_be_recreated(tmp_path, monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def generating(self, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr("run_egoassist.InsightGenerator.generate", generating)

    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "runtime.json"), "unused", "Ethan")
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/auth/register", json={"username": "remove", "password": "remove-password-123"})
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            pending = asyncio.create_task(client.post("/api/insights", json={"sessionId": sid}))
            await asyncio.wait_for(started.wait(), 2)
            response = await client.post("/api/privacy/delete", json={"confirm": True})
            assert response.status == 200
            assert cancelled.is_set()
            assert (await pending).status == 409
    asyncio.run(run())
