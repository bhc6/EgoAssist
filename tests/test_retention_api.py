import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.application.retention import AccountRetention
from egoassist.core.journal import SessionJournal
from run_egoassist import create_app


def test_retention_api_clears_live_cache_and_preserves_other_accounts(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT),
            None,
            str(tmp_path / "runtime.json"),
            "test",
            "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            registered = await client.post(
                "/api/auth/register",
                json={"username": "retention-user", "password": "password123"},
            )
            user = (await registered.json())["user"]["id"]
            created = await client.post("/api/sessions", json={"label": "retention"})
            case_id = (await created.json())["session"]["id"]
            journal = SessionJournal(
                tmp_path / "users" / user / "sessions" / f"{case_id}.jsonl",
                session_id=case_id,
            )
            journal.append(
                "turn_inbox",
                {
                    "turnId": "expired",
                    "text": "expired-secret",
                    "timestamp": (
                        datetime.now(timezone.utc) - timedelta(days=10)
                    ).isoformat(),
                },
            )
            assert (await client.get(f"/api/state?session={case_id}")).status == 200
            result = await client.post(
                "/api/privacy/retention",
                json={"category": "transcript", "retentionDays": 1, "applyNow": True},
            )
            assert result.status == 200, await result.text()
            assert "expired-secret" not in journal.path.read_text(encoding="utf-8")
            state = await client.get(f"/api/state?session={case_id}")
            assert state.status == 200
            assert "expired-secret" not in await state.text()
            policy = await client.post(
                "/api/privacy/retention",
                json={"category": "memory", "retentionDays": 365, "applyNow": True},
            )
            assert policy.status == 200, await policy.text()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_retention_cancellation_or_failure_invalidates_cached_owners(tmp_path, failure):
    async def scenario():
        started = asyncio.Event()
        finish = threading.Event()
        loop = asyncio.get_running_loop()

        def erase(*, user_id):
            loop.call_soon_threadsafe(started.set)
            if not finish.wait(timeout=5):
                raise TimeoutError("test did not release erasure")
            if failure:
                raise OSError("erasure failed")
            return {}

        def owner(user_id):
            return SimpleNamespace(
                user_id=user_id,
                maintenance_task=None,
                job_scheduler_task=None,
                background_tasks={},
            )

        jobs = SimpleNamespace(
            delete_user=AsyncMock(), resume_user=Mock(), recover=Mock()
        )
        retention = AccountRetention(
            governance=SimpleNamespace(retention=erase, user_memory_root=tmp_path),
            identity=Mock(),
            blocked_users=set(),
            invalidate_connections=AsyncMock(),
            insight_jobs=jobs,
            answer_jobs=jobs,
            recovery=jobs,
            owners={"case": owner("user"), "other-case": owner("other")},
            case_memories={("user", "case"): "stale", ("other", "other-case"): "keep"},
            user_memories={"user": "stale", "other": "keep"},
            projections={"case": "stale"},
        )
        task = asyncio.create_task(retention.apply("user"))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            if not failure:
                task.cancel()
                await asyncio.sleep(0)
                assert "user" in retention.blocked_users
                assert not task.done()
            finish.set()
            with pytest.raises(OSError if failure else asyncio.CancelledError):
                await task
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        assert "user" not in retention.blocked_users
        assert set(retention.owners) == {"other-case"}
        assert retention.case_memories == {("other", "other-case"): "keep"}
        assert retention.user_memories == {"other": "keep"}
        assert retention.projections == {}
        jobs.resume_user.assert_called_with("user")

    asyncio.run(scenario())
