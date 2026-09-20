import asyncio
from collections import deque
from copy import deepcopy
from types import SimpleNamespace

import pytest

from egoassist.core.provider_errors import ProviderRequestError
from egoassist.core.journal import SessionJournal
from egoassist.memory import TimelineMemory
from egoassist.memory.recovery import MemoryRecovery, recovery_jobs
from egoassist.prompts import compact_audit_payload
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.omni import OmniBrowserBridge, _observe_access_errors, _OmniPerceptionLane
from egoassist.realtime.scene import SharedScene
from egoassist.application.state import compact_work_item


@pytest.mark.parametrize("status,code,retryable,blocked", [
    (400, "Arrearage", False, True), (401, "InvalidApiKey", False, True),
    (400, "InvalidParameter", False, False), (429, "Throttling", True, False),
    (503, "Unavailable", True, False),
])
def test_failure_classification(status, code, retryable, blocked):
    failure = ProviderRequestError(status, code)
    assert failure.retryable is retryable
    assert failure.account_blocked is blocked


@pytest.mark.parametrize("status,code,expected_calls", [(400, "Arrearage", 1), (429, "Throttling", 2)])
def test_router_account_circuit_and_retryable_limit(status, code, expected_calls):
    async def run():
        calls = []

        class Response:
            def __init__(self):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                return {"code": code, "message": "secret-provider-body", "request_id": "request-1"}

        class Session:
            def post(self, *args, **kwargs):
                calls.append(1)
                return Response()

        router = SemanticTurnRouter("test-key")
        router._http_session = Session()
        one = await router.classify("hello", [])
        two = await router.classify("another", [])
        assert len(calls) == expected_calls
        assert one["providerError"]["code"] == code
        assert "secret-provider-body" not in str(one)
        assert two["retryable"] is (status == 429)
        assert two["requestMetrics"].get("circuitOpen", False) is (status == 400)

    asyncio.run(run())


def test_memory_recovery_does_not_requeue_permanent_failure(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        worker = MemoryRecovery("test", TimelineMemory(tmp_path / "timeline.db"), lambda *_: {})
        failure = ProviderRequestError(400, "Arrearage")

        async def classify(*args):
            return {"status": "unavailable", "retryable": False, "error": str(failure), "providerError": failure.public()}

        worker.router.classify = classify
        worker.submit(journal, user_id="u", turn_id="turn", text="original", source_id="source", clock={})
        await worker.execute(journal, recovery_jobs(journal)["turn"])
        row = recovery_jobs(journal)["turn"]
        assert row["status"] == "failed" and row["attempt"] == 1
        assert row["providerError"]["code"] == "Arrearage"
        worker.router._blocked_failure = failure
        worker.retry(journal, "turn")
        assert worker.router._blocked_failure is None
        assert recovery_jobs(journal)["turn"]["status"] == "queued"
        await worker.close()

    asyncio.run(run())


def test_transport_access_denial_blocks_reconnect_and_audio():
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._blocked_failure = None
    bridge.closed = False
    bridge.connected = True
    bridge._buffered_audio = deque(["old"])
    events = []
    bridge._trace = lambda *args: None
    bridge._schedule = events.append
    original_errors = []
    conversation = SimpleNamespace(_on_error=lambda ws, err: original_errors.append(err))
    _observe_access_errors(conversation, bridge._block_provider)
    for _ in range(2):
        conversation._on_error(None, "Access denied, please make sure your account is in good standing.")
        bridge._schedule_reconnect("provider-closed")
    assert len(events) == 1 and events[0]["retryable"] is False
    assert not bridge.connected and not bridge._buffered_audio
    assert bridge.append_audio("new") is False
    assert len(original_errors) == 2


def test_perception_access_denial_blocks_new_requests():
    lane = _OmniPerceptionLane.__new__(_OmniPerceptionLane)
    lane._blocked_failure = None
    lane.closed = False
    lane.trace = lambda *args: None
    lane._block_provider(ProviderRequestError(400, "Arrearage"))
    lane._schedule_reconnect("provider-closed")
    assert lane.request("image") is False
    assert not lane.connected and not lane.active and lane.pending_frame is None


@pytest.mark.parametrize("kind", ["memory.recovery.result", "memory.recovery.job", "interaction.classification_retry"])
def test_polling_omits_repeated_prompts_without_mutating_journal(kind):
    event = {"turnId": "turn", "scene": {"large": "context"}, "decision": {
        "error": "Arrearage", "requestMetrics": {"messages": [{"content": "long prompt"}],
        "cancellationCheck": {"messages": [{"content": "private context"}]}}, "memoryAtoms": []}}
    original = deepcopy(event)
    compact = compact_audit_payload(kind, event)
    assert event == original
    assert "messages" not in compact["decision"]["requestMetrics"]
    assert compact["decision"]["error"] == "Arrearage"
    assert compact["fullAuditEndpoint"].startswith("/api/prompts")
    if kind == "memory.recovery.job":
        assert "scene" not in compact


def test_scene_snapshot_is_bounded_without_dropping_stored_evidence(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    for index in range(105):
        journal.append("scene.turn", {"turnId": str(index), "speaker": {"status": "unknown"},
            "audio": {"assetId": str(index)}, "observationContext": {"text": "large context"}})
    scene = SharedScene(journal, "")
    snapshot = scene.snapshot()
    assert [r["turnId"] for r in snapshot["utterances"]] == [str(i) for i in range(5, 105)]
    assert snapshot["totalUtterances"] == 105 and snapshot["utterancesTruncated"]
    assert all("observationContext" not in r for r in snapshot["utterances"])
    assert len(scene.turns) == 105 and scene.turns["0"]["observationContext"]
    assert snapshot["utterances"][0]["audio"] == {"assetId": "5"}


def test_work_projection_retains_decisions_without_duplicate_context():
    semantic = {"interaction": {"action": "listen"}, "sourceScene": {"large": "context"},
                "requestMetrics": {"messages": [{"content": "prompt"}]}}
    row = {"turnId": "turn", "text": "full original", "answer": {"text": "full answer"},
           "semantic": semantic, "payload": {"semantic": semantic}}
    original = deepcopy(row)
    value = compact_work_item(row)
    assert row == original
    assert value["text"] == row["text"] and value["answer"] == row["answer"]
    for result in (value["semantic"], value["payload"]["semantic"]):
        assert "sourceScene" not in result
        assert "messages" not in result["requestMetrics"]
        assert result["interaction"] == semantic["interaction"]
