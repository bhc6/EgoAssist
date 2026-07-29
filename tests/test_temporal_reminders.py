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
