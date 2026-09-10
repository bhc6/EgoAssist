"""Durable memory-only recovery; never replay dialogue controls or tool effects."""
import asyncio
from datetime import datetime
import hashlib
import time

from egoassist.memory.evidence import ground_context
from egoassist.memory.temporal import ClockContext, resolve_time_semantics
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.settings import integer, number


def recovery_jobs(journal):
    return {row["turnId"]: row for row in journal.entries() if row.get("type") == "memory.recovery.job"}


class MemoryRecovery:
    def __init__(self, api_key, timeline, policy):
        self.router = SemanticTurnRouter(api_key, timeout_seconds=number("EGOASSIST_MEMORY_RECOVERY_TIMEOUT", 20))
        self.timeline, self.policy = timeline, policy
        self.journals, self.tasks = {}, {}
        self.attempts = integer("EGOASSIST_MEMORY_RECOVERY_ATTEMPTS", 3, maximum=20)
        self.concurrency = integer("EGOASSIST_MEMORY_RECOVERY_CONCURRENCY", 1, maximum=8)
        self.delay = number("EGOASSIST_MEMORY_RECOVERY_DELAY", 5)

    def update(self, journal, row, **changes):
        return journal.append("memory.recovery.job", {
            **{k: v for k, v in row.items() if k not in {"id", "type", "timestamp", "parentId"}},
            **changes,
        }, parent_id=row.get("id"))

    def submit(self, journal, *, user_id, turn_id, text, source_id, clock, draft=None, scene=None):
        self.journals[str(journal.path)] = journal
        if turn_id not in recovery_jobs(journal):
            journal.append("memory.recovery.job", {
                "turnId": turn_id, "userId": user_id, "text": text, "sourceId": source_id,
                "clock": clock, "draft": draft, "scene": scene,
                "status": "queued", "attempt": 0, "nextAttemptAt": time.time() + self.delay,
            }, idempotency_key="memory-recovery:" + turn_id)

    def recover(self, journal):
        self.journals[str(journal.path)] = journal
        for row in recovery_jobs(journal).values():
            if row["status"] == "running":
                self.update(journal, row, status="queued", nextAttemptAt=time.time())

    def retry(self, journal, turn_id):
        row = recovery_jobs(journal).get(turn_id)
        if not row or row["status"] != "failed":
            raise ValueError("only failed memory recovery can be retried")
        self.journals[str(journal.path)] = journal
        self.update(journal, row, status="queued", attempt=0, nextAttemptAt=time.time(), error=None)

    async def execute(self, journal, row):
        row = recovery_jobs(journal).get(row["turnId"], row)
        if row["status"] == "completed":
            return
        row = self.update(journal, row, status="running", attempt=row["attempt"] + 1)
        try:
            self.router.set_turn_context(clock_context=row["clock"], memory_draft=row.get("draft"), scene=row.get("scene"))
            decision = row.get("decision")
            if decision is None:
                decision = await self.router.classify(row["text"], [])
                journal.append("memory.recovery.result", {"turnId": row["turnId"], "decision": decision, "model": self.router.model})
            if decision["status"] != "classified" or decision.get("componentErrors"):
                raise ValueError(decision.get("error") or "invalid memory components")
            row = self.update(journal, row, decision=decision)
            clock = ClockContext.current(row["clock"]["timezone"], row["clock"]["utcOffsetMinutes"],
                                         now_utc=datetime.fromisoformat(row["clock"]["nowUtc"]))
            policy = self.policy(row["userId"], journal.session_id)["components"]["timeline"]
            ids = []
            for index, atom in enumerate(decision.get("memoryAtoms") or []):
                scope = atom["scope"]
                if not policy["preference_enabled" if scope == "participant" else "episodic_enabled"]:
                    continue
                temporal = resolve_time_semantics(atom["text"], clock, kind=atom["kind"])
                context = ground_context(atom.get("context"), row["text"], clock)
                context.update({"sourceText": row["text"], "speaker": (row.get("scene") or {}).get("speaker"),
                                "recoveredAt": datetime.now().astimezone().isoformat()})
                card = self.timeline.remember(
                    atom["text"], user_id=row["userId"], case_id=journal.session_id, scope=scope,
                    kind=atom["kind"], tier="long" if scope == "participant" and atom["consent"] == "explicit" else "short",
                    status="pending" if atom["consent"] == "inferred" else "active",
                    source_ids=[row["sourceId"]], recorded_at=temporal["recordedAt"],
                    effective_at=temporal["effectiveAt"], effective_end=temporal["effectiveEnd"],
                    timezone_name=clock.timezone_name, utc_offset_minutes=clock.utc_offset_minutes,
                    subject=atom.get("subject"), predicate=atom.get("predicate"), value=atom.get("value"),
                    metadata={"source": "semantic-memory-recovery", "evidenceContext": context},
                    supersede=False,
                    replaces_card_id=atom.get("replaces"),
                    card_id=hashlib.sha256(f'{journal.session_id}:{row["turnId"]}:{index}'.encode()).hexdigest()[:24])
                if card:
                    ids.append(card["id"])
            pending = self.timeline.pending_draft(user_id=row["userId"], case_id=journal.session_id)
            draft = decision.get("memoryDraft")
            if draft and not pending and policy["preference_enabled"]:
                self.timeline.begin_draft(user_id=row["userId"], case_id=journal.session_id,
                    subject=draft.get("subject"), predicate=draft["predicate"],
                    missing_fields=draft["missingFields"], language=draft.get("language"), source_ids=[row["sourceId"]])
            if ids and pending and row.get("draft") and pending["id"] == row["draft"].get("id"):
                if any((pending.get("predicate") == "memory.value" and atom["consent"] == "explicit") or
                       self.timeline.draft_accepts(pending, subject=atom.get("subject"), predicate=atom.get("predicate"))
                       for atom in decision.get("memoryAtoms") or []):
                    self.timeline.finish_draft(pending["id"], status="consumed")
            self.update(journal, row, status="completed", cardIds=ids,
                        draftCandidate=decision.get("memoryDraft"), error=None)
        except asyncio.CancelledError:
            self.update(journal, row, status="queued", nextAttemptAt=time.time())
            raise
        except Exception as exc:
            self.update(journal, row, status="failed" if row["attempt"] >= self.attempts else "queued",
                        error=type(exc).__name__ + ": " + str(exc)[:200],
                        nextAttemptAt=time.time() + self.delay * row["attempt"])

    async def run(self):
        while True:
            for journal in list(self.journals.values()):
                jobs = recovery_jobs(journal)
                if not any(row["status"] in {"queued", "running"} for row in jobs.values()):
                    self.journals.pop(str(journal.path), None)
                    continue
                for row in jobs.values():
                    key = (str(journal.path), row["turnId"])
                    if len(self.tasks) >= self.concurrency:
                        break
                    if key not in self.tasks and row["status"] == "queued" and row["nextAttemptAt"] <= time.time():
                        task = asyncio.create_task(self.execute(journal, row))
                        self.tasks[key] = task
                        def finished(done, key=key):
                            self.tasks.pop(key, None)
                            if not done.cancelled() and done.exception():
                                asyncio.get_running_loop().call_exception_handler({
                                    "message": "Memory recovery persistence failed", "exception": done.exception()})
                        task.add_done_callback(finished)
            await asyncio.sleep(0.5)

    async def delete_user(self, user_id):
        paths = {path for path, journal in self.journals.items()
                 if any(r["userId"] == user_id for r in recovery_jobs(journal).values())}
        tasks = [task for (path, _), task in self.tasks.items() if path in paths]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for path in paths:
            self.journals.pop(path, None)

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
        await self.router.aclose()
