import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from temporalio.testing import WorkflowEnvironment
from temporalio.client import Client
from temporalio.worker import Worker
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.product import NotificationReceipt
from egoassist.product.reminders import (
    DEFAULT_TASK_QUEUE,
    ReminderActivities,
    ReminderWorkflow,
    ReminderWorkflowInput,
    TemporalReminderCoordinator,
)
from egoassist.realtime import CaseWorkLedger, SessionJournal
import egoassist.realtime.omni as omni_module
import run_egoassist
from run_egoassist import create_app


ROOT = Path(__file__).parents[1]


class RecordingNotificationPort:
    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return NotificationReceipt(
            notification_id=request.notification_id,
            channel=request.channel,
            status="acknowledged",
            provider_message_id="ntfy-test-1",
        )


def reminder_fixture(tmp_path, *, due_at, suffix="1"):
    journal_path = tmp_path / f"case-{suffix}.jsonl"
    ledger = CaseWorkLedger(SessionJournal(journal_path, session_id=f"case-{suffix}"))
    turn = ledger.capture_turn("remind me", source="test")
    intent = ledger.create_intent(
        turn["turnId"], kind="background-reminder", status="running-background",
    )
    workflow_id = f"egoassist-reminder-{suffix}"
    job = ledger.create_job(
        intent["intentId"],
        kind="timer-delivery",
        payload={"dueAt": due_at.isoformat(), "label": "check rice", "workflowId": workflow_id},
        status="running",
    )
    value = ReminderWorkflowInput(
        workflow_id=workflow_id,
        user_id="user-1",
        session_id=f"case-{suffix}",
        journal_path=str(journal_path),
        intent_id=intent["intentId"],
        job_id=job["jobId"],
        result_id=f"reminder-result-{suffix}",
        label="check rice",
        due_at=due_at.isoformat(),
    )
    return ledger, value


def test_activity_is_idempotent_for_result_and_notification(tmp_path):
    async def scenario():
        port = RecordingNotificationPort()
        activities = ReminderActivities(port)
        ledger, value = reminder_fixture(tmp_path, due_at=datetime.now(timezone.utc))
        first = await activities.deliver(value.__dict__)
        second = await activities.deliver(value.__dict__)
        restored = CaseWorkLedger(SessionJournal(value.journal_path, session_id=value.session_id))
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert len(restored.results()) == 1
        assert len(port.requests) == 1
        assert restored.jobs()[value.job_id]["status"] == "completed"

    asyncio.run(scenario())


def test_temporal_reminder_survives_worker_restart_and_cancel_is_distinct(tmp_path):
    async def scenario():
        environment = await WorkflowEnvironment.start_time_skipping(
            download_dest_dir=str(ROOT / ".cache" / "temporal-test-server"),
        )
        try:
            port = RecordingNotificationPort()
            activities = ReminderActivities(port)
            now = await environment.get_current_time()
            ledger, value = reminder_fixture(tmp_path, due_at=now + timedelta(hours=1), suffix="restart")
            coordinator = TemporalReminderCoordinator(environment.client, task_queue=DEFAULT_TASK_QUEUE)

            async with Worker(
                environment.client,
                task_queue=DEFAULT_TASK_QUEUE,
                workflows=[ReminderWorkflow],
                activities=[activities.deliver],
                max_cached_workflows=0,
            ):
                await coordinator.start(value)
                assert (await coordinator.state(value.workflow_id))["status"] in {"created", "scheduled"}

            restart_client = await Client.connect(
                environment.client.service_client.config.target_host,
                namespace="default",
            )
            coordinator = TemporalReminderCoordinator(restart_client, task_queue=DEFAULT_TASK_QUEUE)
            async with Worker(
                restart_client,
                task_queue=DEFAULT_TASK_QUEUE,
                workflows=[ReminderWorkflow],
                activities=[activities.deliver],
                max_cached_workflows=0,
            ):
                await asyncio.sleep(0.1)
                await environment.sleep(timedelta(hours=1))
                await asyncio.sleep(0.1)
                assert (await coordinator.state(value.workflow_id))["status"] in {"firing", "completed"}
                completed = await restart_client.get_workflow_handle(value.workflow_id).result()
                assert completed["status"] == "completed"

                _, cancelled_value = reminder_fixture(
                    tmp_path, due_at=(await environment.get_current_time()) + timedelta(days=1), suffix="cancel",
                )
                await coordinator.start(cancelled_value)
                await coordinator.cancel(cancelled_value.workflow_id)
                cancelled = await restart_client.get_workflow_handle(cancelled_value.workflow_id).result()
                assert cancelled["status"] == "cancelled"

            restored = CaseWorkLedger(SessionJournal(value.journal_path, session_id=value.session_id))
            assert len(restored.pending_results()) == 1
            assert restored.pending_results()[0]["resultId"] == value.result_id
            assert len(port.requests) == 1
        finally:
            await environment.shutdown()

    asyncio.run(scenario())


def test_product_api_exposes_timer_only_with_live_temporal_worker(tmp_path, monkeypatch):
    async def scenario():
        environment = await WorkflowEnvironment.start_time_skipping(
            download_dest_dir=str(ROOT / ".cache" / "temporal-test-server"),
        )
        try:
            monkeypatch.setenv(
                "EGOASSIST_TEMPORAL_TARGET",
                environment.client.service_client.config.target_host,
            )
            app = create_app(
                str(BUILTIN_SKILLS_ROOT), None,
                str(tmp_path / "memory.json"), "test-realtime", "Ethan",
            )
            async with TestClient(TestServer(app)) as client:
                registered = await client.post("/api/auth/register", json={
                    "username": "timer-user", "password": "correct horse battery staple",
                })
                assert registered.status == 200
                session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
                activated = await client.post("/api/skills/activate", json={
                    "sessionId": session_id, "skill": "kitchen-coach",
                })
                state = await activated.json()
                timer = next(item for item in state["capabilityCatalog"]["platform"] if item["id"] == "timer-delivery")
                assert timer["status"] == "available"
                assert {item["name"] for item in state["activeSkill"]["tools"] if item["availability"]["executable"]} == {"set_timer"}

                created = await client.post("/api/reminders", json={
                    "sessionId": session_id, "seconds": 3600, "label": "check the oven",
                })
                assert created.status == 200
                payload = await created.json()
                job_id = payload["reminder"]["jobId"]
                assert payload["reminder"]["status"] == "queued"
                await asyncio.sleep(0.1)
                await environment.sleep(timedelta(hours=1))
                await asyncio.sleep(0.1)
                fired = await (await client.get(f"/api/state?session={session_id}")).json()
                job = next(item for item in fired["workLedger"]["jobs"] if item["jobId"] == job_id)
                assert job["status"] == "completed"
                results = [item for item in fired["workLedger"]["resultInbox"] if item["jobId"] == job_id]
                assert len(results) == 1
                assert results[0]["status"] == "pending"

                cancel_created = await (await client.post("/api/reminders", json={
                    "sessionId": session_id, "seconds": 7200, "label": "cancel me",
                })).json()
                cancel_job_id = cancel_created["reminder"]["jobId"]
                cancelled = await client.post("/api/jobs/cancel", json={
                    "sessionId": session_id, "jobId": cancel_job_id,
                })
                assert cancelled.status == 200
                assert (await cancelled.json())["job"]["status"] == "cancelled"
        finally:
            await environment.shutdown()

    asyncio.run(scenario())


def test_multi_intent_timer_and_memory_draft_recover_after_disconnect(tmp_path, monkeypatch):
    bridge_instances = []

    class FakeBridge:
        def __init__(self, _api_key, _model, _voice, _instructions, emit, on_transcript):
            self.emit = emit
            self.on_transcript = on_transcript
            self.requests = []
            bridge_instances.append(self)

        async def connect(self):
            await self.emit({"type": "ready"})

        async def close(self):
            return None

        def set_context_harness(self, value): self.harness = value
        def set_tool_result_handler(self, value): self.tool_result_handler = value
        def set_result_delivery_handler(self, value): self.result_delivery_handler = value
        def set_response_lifecycle_handler(self, value): self.lifecycle_handler = value
        def set_tools(self, declarations, handler): self.tools = (declarations, handler)
        def set_trace_sink(self, value): self.trace_sink = value
        def update_context(self, value): self.context = value
        def apply_context_projection(self, value, manifest): self.context = value
        def inject_context_data(self, _label, _value): return True
        def append_audio(self, _value): return False
        def append_video(self, _value): return False
        def maybe_check_proactively(self, _interval): return False
        def has_unresolved_user_input(self): return False
        def cancel_response(self, **_kwargs): return True
        def handle_user_speech_started(self, **_kwargs): return "immediate"
        def request_response(self, instructions, **kwargs):
            self.requests.append((instructions, kwargs))
            return True

    def base_semantic(text):
        return {
            "method": "semantic-turn-router-v6",
            "model": "test-router",
            "status": "classified",
            "speechControl": None,
            "taskControl": None,
            "preferenceIntent": None,
            "memoryQuery": None,
            "memoryKind": None,
            "memoryText": None,
            "memoryKey": None,
            "memoryScope": None,
            "memoryConsent": None,
            "memoryAtoms": [],
            "memoryDraft": None,
            "memoryQuerySpec": None,
            "skillControl": None,
            "selectedSkill": None,
            "intentKind": "foreground-dialogue",
            "intents": [],
            "skillStatus": "no-match",
            "rawOutput": text,
            "latencyMs": 0.1,
            "policyVersion": "semantic-turn-router-v6",
            "claimBoundary": "test",
        }

    async def classify(_self, text, _catalog):
        result = base_semantic(text)
        if text.startswith("remind me"):
            result["intents"] = [
                {
                    "kind": "background-reminder",
                    "text": "remind me in 60 seconds to check the oven",
                    "parameters": {"delaySeconds": 60, "label": "check the oven"},
                },
                {
                    "kind": "foreground-dialogue",
                    "text": "what temperature should I use",
                    "parameters": {},
                },
            ]
        else:
            result.update({
                "memoryKind": "capture",
                "memoryScope": "participant",
                "memoryConsent": "explicit",
                "memoryDraft": {
                    "mode": "start",
                    "subject": "user",
                    "predicate": "phone",
                    "language": "en",
                    "missingFields": ["value"],
                },
                "intentKind": "waiting-input",
                "intents": [{
                    "kind": "waiting-input",
                    "text": text,
                    "parameters": {},
                }],
            })
        return result

    async def receive_state(ws):
        while True:
            message = await asyncio.wait_for(ws.receive_json(), timeout=2)
            if message.get("type") == "state":
                return message["state"]

    async def scenario():
        environment = await WorkflowEnvironment.start_time_skipping(
            download_dest_dir=str(ROOT / ".cache" / "temporal-test-server"),
        )
        try:
            monkeypatch.setenv(
                "EGOASSIST_TEMPORAL_TARGET",
                environment.client.service_client.config.target_host,
            )
            monkeypatch.setenv("QWEN_API_KEY", "test-key")
            monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)
            monkeypatch.setattr(omni_module, "OmniBrowserBridge", FakeBridge)
            app = create_app(
                str(BUILTIN_SKILLS_ROOT), None,
                str(tmp_path / "memory.json"), "test-realtime", "Ethan",
            )
            async with TestClient(TestServer(app)) as client:
                registered = await client.post("/api/auth/register", json={
                    "username": "multi-recovery", "password": "multi-recovery-pass",
                })
                user = (await registered.json())["user"]
                session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
                ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
                await ws.receive_json()
                await receive_state(ws)

                bridge_instances[-1].on_transcript(
                    "remind me in 60 seconds to check the oven, and what temperature should I use?",
                )
                first = await receive_state(ws)
                kinds = [item["kind"] for item in first["workLedger"]["intents"]]
                assert "background-reminder" in kinds
                assert "foreground-dialogue" in kinds
                reminder_job = next(
                    item for item in first["workLedger"]["jobs"]
                    if item["kind"] == "timer-delivery"
                )
                assert reminder_job["status"] in {"queued", "running"}
                assert any("Do not call set_timer again" in request[0] for request in bridge_instances[-1].requests)

                bridge_instances[-1].on_transcript("remember my phone number")
                second = await receive_state(ws)
                waiting = [
                    item for item in second["workLedger"]["intents"]
                    if item["status"] == "waiting-input"
                ]
                assert len(waiting) == 1
                waiting_id = waiting[0]["intentId"]
                await ws.close()

                await asyncio.sleep(0.1)
                await environment.sleep(timedelta(seconds=60))
                await asyncio.sleep(0.2)

                reconnected = await client.ws_connect(f"/ws/realtime?session={session_id}")
                await reconnected.receive_json()
                recovered = await receive_state(reconnected)
                intents = {item["intentId"]: item for item in recovered["workLedger"]["intents"]}
                jobs = {item["jobId"]: item for item in recovered["workLedger"]["jobs"]}
                results = recovered["workLedger"]["resultInbox"]
                assert intents[waiting_id]["status"] == "waiting-input"
                assert jobs[reminder_job["jobId"]]["status"] == "completed"
                reminder_results = [
                    item for item in results if item["jobId"] == reminder_job["jobId"]
                ]
                assert len(reminder_results) == 1
                assert reminder_results[0]["intentId"] == reminder_job["intentId"]
                assert reminder_results[0]["status"] == "pending"
                assert recovered["episodicMemory"]["timelineViews"]["case"]["draft"] is not None
                await reconnected.close()
        finally:
            await environment.shutdown()

    asyncio.run(scenario())
