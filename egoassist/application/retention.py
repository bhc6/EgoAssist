"""Quiesce account writers, erase expired evidence, then recover surviving jobs."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from egoassist.core.journal import SessionJournal
from egoassist.core.settings import number


@dataclass
class AccountRetention:
    governance: Any
    identity: Any
    blocked_users: set
    invalidate_connections: Any
    insight_jobs: Any
    answer_jobs: Any
    recovery: Any
    owners: Any
    case_memories: Any
    user_memories: Any
    projections: Any
    active_requests: dict = field(default_factory=dict)
    derived_stores: tuple = ()

    async def apply(self, user_id):
        if user_id in self.blocked_users:
            raise ValueError("account data maintenance already in progress")
        self.blocked_users.add(user_id)
        try:
            requests = [
                task
                for task in tuple(self.active_requests.get(user_id, ()))
                if task is not asyncio.current_task() and not task.done()
            ]
            for task in requests:
                task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            await self.invalidate_connections(user_id)
            await self.insight_jobs.delete_user(user_id)
            await self.answer_jobs.delete_user(user_id)
            await self.recovery.delete_user(user_id)
            for derived in self.derived_stores:
                await derived.pause_user(user_id)
            background = []
            for owner in self.owners.values():
                if owner.user_id != user_id:
                    continue
                for task in (owner.maintenance_task, owner.job_scheduler_task):
                    if task is not None and not task.done():
                        background.append(task)
                background.extend(
                    slot.asyncio_task
                    for slot in owner.background_tasks.values()
                    if slot.asyncio_task is not None and not slot.asyncio_task.done()
                )
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            erasure = asyncio.create_task(
                asyncio.to_thread(self.governance.retention, user_id=user_id)
            )
            try:
                result = await asyncio.shield(erasure)
            except asyncio.CancelledError:
                await erasure
                raise
            self.insight_jobs.resume_user(user_id)
            for path in (self.governance.user_memory_root / user_id / "sessions").glob(
                "*.jsonl"
            ):
                try:
                    self.identity.get_context_session(user_id, path.stem)
                except (ValueError, KeyError):
                    continue
                journal = SessionJournal(path)
                self.insight_jobs.recover(user_id, path.stem, journal)
                self.answer_jobs.recover(user_id, journal)
                self.recovery.recover(journal)
            return result
        finally:
            # Erasure may have completed even when its caller was cancelled.
            # Never allow a surviving owner to serve pre-erasure cached data.
            for case_id, owner in list(self.owners.items()):
                if owner.user_id == user_id:
                    self.owners.pop(case_id, None)
                    self.case_memories.pop((user_id, case_id), None)
            self.user_memories.pop(user_id, None)
            self.projections.clear()
            self.insight_jobs.resume_user(user_id)
            self.blocked_users.discard(user_id)
            for derived in self.derived_stores:
                derived.invalidate_user(user_id)
                derived.resume_user(user_id)

    async def run(self):
        interval = number(
            "EGOASSIST_RETENTION_INTERVAL", 3600, minimum=60, maximum=86400
        )
        while True:
            await asyncio.sleep(interval)
            users = {
                row["user_id"]
                for row in self.governance.product_database.retention_policies()
            }
            for user_id in users:
                if user_id in self.blocked_users:
                    continue
                try:
                    await self.apply(user_id)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Account retention did not complete"
                    )
