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


async def finished(client, sid, job_id):
    async with asyncio.timeout(5):
        while True:
            payload = await (await client.get("/api/insights", params={"session": sid})).json()
            job = next(item for item in payload["jobs"] if item["jobId"] == job_id)
            if job["status"] not in {"queued", "running"}:
                return payload
            await asyncio.sleep(0.01)


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


def test_ambient_report_uses_sample_time_and_keeps_uncertainty(tmp_path):
    journal = SessionJournal(tmp_path / "ambient.jsonl", session_id="ambient")
    journal.append("ambient_observation", {"status": "accepted", "evidence": {
        "capturedAt": "2026-09-09T15:59:59+00:00", "visibleEvidence": ["a hand holds a cup"],
        "uncertainties": ["contents unknown"], "media": {"sha256": "frame-hash"}}})
    clock = ClockContext.current("Asia/Shanghai")
    records = collect_sources([journal.path], calendar_period("daily", "2026-09-09", clock))["records"]
    assert len(records) == 1 and records[0]["sourceKind"] == "visual-observation"
    assert "contents unknown" in records[0]["text"] and "frame-hash" in records[0]["text"]
    assert not collect_sources([journal.path], calendar_period("daily", "2026-09-10", clock))["records"]


def test_report_sources_keep_utc_and_local_midnight_anchor(tmp_path):
    path = tmp_path / "midnight.jsonl"
    row = {"type": "message", "id": "source", "status": "completed",
           "timestamp": "2026-09-09T16:30:00+00:00",
           "message": {"role": "user", "content": "今天收到样机，验收未完成。"}}
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    clock = ClockContext.current("Asia/Shanghai", now_utc=datetime(2026, 9, 9, 17, tzinfo=timezone.utc))
    source = collect_sources([path], calendar_period("daily", None, clock))["records"][0]
    assert source["recordedAt"] == row["timestamp"]
    assert source["recordedLocalAt"] == "2026-09-10T00:30:00+08:00"
    assert source["text"] == row["message"]["content"]


def test_insight_endpoints_enforce_owner_and_auto_policy(tmp_path):
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "runtime.json"), "unused", "Ethan")
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/auth/register", json={"username": "owner", "password": "owner-password-123"})
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            response = await client.post("/api/insights", json={"sessionId": sid, "kind": "daily"})
            assert response.status == 202
            job_id = (await response.json())["job"]["jobId"]
            result = (await finished(client, sid, job_id))["results"][0]
            assert result["status"] == "empty"
            assert result["coverage"]["complete"] is True
            again = await (await client.post("/api/insights", json={"sessionId": sid, "kind": "daily"})).json()
            cached = await finished(client, sid, again["job"]["jobId"])
            assert cached["jobs"][-1]["resultEventId"] == result["id"]
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
            accepted = await client.post("/api/insights", json={"sessionId": sid})
            assert accepted.status == 202
            await asyncio.wait_for(started.wait(), 2)
            assert (await client.get("/api/state", params={"session": sid})).status == 200
            response = await client.post("/api/privacy/delete", json={"confirm": True})
            assert response.status == 200
            assert cancelled.is_set()
    asyncio.run(run())


def test_report_queue_deduplicates_cancels_and_recovers(tmp_path):
    from egoassist.product.insight_jobs import InsightJobs, InsightQueueFull, insight_jobs

    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        started = asyncio.Event()

        class Generator:
            async def generate(self, **kwargs):
                started.set()
                await asyncio.Future()

        manager = InsightJobs(Generator(), lambda *args: [journal.path], capacity=2)
        params = dict(user_id="u", session_id="case", journal=journal, scope="case", anchor=None,
                      clock=ClockContext.current("UTC"))
        first = manager.submit(**params, kind="daily")
        duplicate = manager.submit(**params, kind="daily")
        assert duplicate["jobId"] == first["jobId"]
        second = manager.submit(**params, kind="weekly")
        with pytest.raises(InsightQueueFull):
            manager.submit(**params, kind="monthly")
        await started.wait()
        await manager.cancel(second["jobId"], journal)
        assert insight_jobs(journal)[-1]["status"] == "cancelled"
        await manager.close()
        assert insight_jobs(journal)[0]["status"] == "queued"

        class RecoveredGenerator:
            async def generate(self, **kwargs):
                return journal.append("insight.result", {"status": "completed"})

        restored = InsightJobs(RecoveredGenerator(), lambda *args: [journal.path])
        restored.recover("u", "case", journal)
        await asyncio.gather(*list(restored._tasks.values()))
        assert insight_jobs(journal)[0]["status"] == "completed"
        assert insight_jobs(journal)[0]["attempt"] == 2
        await restored.close()
    asyncio.run(run())


def test_report_timeout_releases_worker_for_next_job(tmp_path):
    from egoassist.product.insight_jobs import InsightJobs, insight_jobs

    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")

        class Generator:
            async def generate(self, **kwargs):
                if kwargs["kind"] == "daily":
                    await asyncio.Future()
                return journal.append("insight.result", {"status": "completed"})

        manager = InsightJobs(Generator(), lambda *args: [journal.path])
        manager.timeout = 0.02
        params = dict(user_id="u", session_id="case", journal=journal, scope="case", anchor=None,
                      clock=ClockContext.current("UTC"))
        manager.submit(**params, kind="daily")
        manager.submit(**params, kind="weekly")
        await asyncio.gather(*list(manager._tasks.values()))
        failed, completed = insight_jobs(journal)
        assert failed["status"] == "failed" and failed["timedOut"]
        assert completed["status"] == "completed"
        await manager.close()
    asyncio.run(run())


def test_restart_recovers_all_pending_without_browser_or_attempt_limit(tmp_path):
    from egoassist.product.insight_jobs import InsightJobs, insight_jobs, visible_insight_jobs

    async def run():
        journal = SessionJournal(tmp_path / "recovery.jsonl", session_id="recovery")
        for i in range(20):
            journal.append("insight.job", {"jobId": str(i), "status": "running", "attempt": 4,
                "specification": {"kind": "daily", "scope": "case", "anchor": "2026-09-09",
                                  "timezone": "UTC", "utcOffsetMinutes": 0}})
        assert len(visible_insight_jobs(journal)) == 20
        completed = asyncio.Event()
        active = maximum = count = 0

        class Generator:
            async def generate(self, **kwargs):
                nonlocal active, maximum, count
                active += 1
                maximum = max(maximum, active)
                kwargs["progress"]("waiting-provider", model="test-only")
                await asyncio.sleep(0.005)
                active -= 1
                count += 1
                if count == 20:
                    completed.set()
                return journal.append("insight.result", {"status": "completed"})

        manager = InsightJobs(Generator(), lambda *args: [journal.path], concurrency=2, capacity=3)
        manager.recover("u", "recovery", journal)
        await asyncio.wait_for(completed.wait(), 3)
        await asyncio.sleep(0)
        assert maximum == 2
        assert all(row["status"] == "completed" and row["attempt"] == 5 for row in insight_jobs(journal))
        assert not manager._deferred
        await manager.close()
    asyncio.run(run())
