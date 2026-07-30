"""Temporal-owned reminder workflow and idempotent ResultInbox delivery."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from temporalio import activity, workflow  # type: ignore[import-untyped]
    from temporalio.client import Client  # type: ignore[import-untyped]
    from temporalio.common import RetryPolicy, WorkflowIDReusePolicy  # type: ignore[import-untyped]
    _TEMPORAL_AVAILABLE = True
except ImportError:
    _TEMPORAL_AVAILABLE = False

    class _Stub:  # type: ignore[no-untyped-def]
        """Pass-through stub for temporalio decorators and context managers."""
        def __getattr__(self, name: str) -> "_Stub":
            return _Stub()
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            # If used as a bare decorator, pass the decorated object through.
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return _Stub()
        def __enter__(self) -> "_Stub":
            return self
        def __exit__(self, *args: Any) -> bool:
            return False

    activity = _Stub()  # type: ignore[assignment]
    workflow = _Stub()  # type: ignore[assignment]
    Client = None  # type: ignore[assignment,misc]
    RetryPolicy = None  # type: ignore[assignment,misc]
    WorkflowIDReusePolicy = None  # type: ignore[assignment,misc]

with workflow.unsafe.imports_passed_through():
    from pathlib import Path

    from egoassist.realtime.context_harness import SessionJournal
    from egoassist.realtime.work_ledger import CaseWorkLedger

    from .contracts import NotificationRequest
    from .ports import NotificationPort


REMINDER_WORKFLOW_NAME = "egoassist.reminder.v1"
REMINDER_ACTIVITY_NAME = "egoassist.deliver-reminder.v1"
DEFAULT_TASK_QUEUE = "egoassist-reminders-v1"


@dataclass(frozen=True)
class ReminderWorkflowInput:
    workflow_id: str
    user_id: str
    session_id: str
    journal_path: str
    intent_id: str
    job_id: str
    result_id: str
    label: str
    due_at: str


@workflow.defn(name=REMINDER_WORKFLOW_NAME)
class ReminderWorkflow:
    def __init__(self) -> None:
        self._status = "created"
        self._cancel_reason: str | None = None

    @workflow.run
    async def run(self, value: ReminderWorkflowInput) -> dict[str, Any]:
        due_at = datetime.fromisoformat(value.due_at.replace("Z", "+00:00"))
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        delay = max(0.0, (due_at - workflow.now()).total_seconds())
        self._status = "scheduled"
        if delay:
            try:
                await workflow.wait_condition(
                    lambda: self._cancel_reason is not None,
                    timeout=timedelta(seconds=delay),
                    timeout_summary="reminder deadline",
                )
            except asyncio.TimeoutError:
                pass
        if self._cancel_reason is not None:
            self._status = "cancelled"
            return {"status": "cancelled", "reason": self._cancel_reason}
        self._status = "firing"
        receipt = await workflow.execute_activity(
            REMINDER_ACTIVITY_NAME,
            asdict(value),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(minutes=1),
                maximum_attempts=8,
            ),
        )
        self._status = "completed"
        return dict(receipt)

    @workflow.signal
    async def cancel(self, reason: str = "explicit-user-cancel") -> None:
        self._cancel_reason = str(reason)

    @workflow.query
    def state(self) -> dict[str, Any]:
        return {"status": self._status, "cancelReason": self._cancel_reason}


class ReminderActivities:
    def __init__(self, notification: NotificationPort | None = None) -> None:
        self.notification = notification

    @activity.defn(name=REMINDER_ACTIVITY_NAME)
    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = ReminderWorkflowInput(**payload)
        journal = SessionJournal(
            Path(value.journal_path),
            session_id=value.session_id,
        )
        ledger = CaseWorkLedger(journal)
        existing = ledger.results().get(value.result_id)
        duplicate = existing is not None
        if existing is None:
            ledger.enqueue_result(
                value.intent_id,
                {"kind": "reminder", "label": value.label, "dueAt": value.due_at},
                job_id=value.job_id,
                priority=100,
                result_id=value.result_id,
            )
        job = ledger.jobs().get(value.job_id)
        if job and job.get("status") not in {"completed", "failed", "cancelled"}:
            ledger.set_job_status(
                value.job_id,
                "completed",
                detail={"resultId": value.result_id, "workflowId": value.workflow_id},
            )

        notification_receipt: dict[str, Any] | None = None
        delivery_key = f"notification:{value.result_id}"
        prior_delivery = next((
            entry for entry in reversed(journal.entries())
            if entry.get("type") == "notification_delivery"
            and entry.get("resultId") == value.result_id
        ), None)
        prior_receipt = dict((prior_delivery or {}).get("receipt") or {})
        if self.notification is not None and prior_receipt.get("status") != "acknowledged":
            try:
                attempt = activity.info().attempt
            except RuntimeError:
                attempt = 1
            request = NotificationRequest(
                notification_id=value.result_id,
                user_id=value.user_id,
                result_id=value.result_id,
                channel="ntfy",
                deduplication_key=delivery_key,
                title="EgoAssist reminder",
                body=value.label,
            )
            receipt = await self.notification.send(request)
            notification_receipt = receipt.model_dump(mode="json")
            journal.append(
                "notification_delivery",
                {
                    "resultId": value.result_id,
                    "receipt": notification_receipt,
                    "attempt": attempt,
                },
                idempotency_key=f"{delivery_key}:attempt:{attempt}",
            )
            if receipt.status == "failed":
                raise RuntimeError(receipt.error or "notification delivery failed")
        elif prior_delivery is not None:
            notification_receipt = prior_receipt
        return {
            "status": "completed",
            "resultId": value.result_id,
            "duplicate": duplicate,
            "notification": notification_receipt,
        }


class TemporalReminderCoordinator:
    def __init__(self, client: Client, *, task_queue: str = DEFAULT_TASK_QUEUE) -> None:
        self.client = client
        self.task_queue = str(task_queue)

    @classmethod
    async def connect(
        cls,
        target: str,
        *,
        namespace: str = "default",
        task_queue: str = DEFAULT_TASK_QUEUE,
    ) -> "TemporalReminderCoordinator":
        client = await Client.connect(str(target), namespace=str(namespace))
        return cls(client, task_queue=task_queue)

    async def start(self, value: ReminderWorkflowInput) -> str:
        await self.client.start_workflow(
            ReminderWorkflow.run,
            value,
            id=value.workflow_id,
            task_queue=self.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            static_summary=f"EgoAssist reminder: {value.label[:80]}",
        )
        return value.workflow_id

    async def cancel(self, workflow_id: str, *, reason: str = "explicit-user-cancel") -> None:
        await self.client.get_workflow_handle(str(workflow_id)).signal(ReminderWorkflow.cancel, reason)

    async def state(self, workflow_id: str) -> dict[str, Any]:
        return await self.client.get_workflow_handle(str(workflow_id)).query(ReminderWorkflow.state)
