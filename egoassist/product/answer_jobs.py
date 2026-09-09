"""Finish interrupted answers without replaying user controls or tool actions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import time

import aiohttp

from egoassist.prompts import prompt_text
from egoassist.realtime.work_ledger import CaseWorkLedger


def answer_jobs(journal):
    latest = {}
    for entry in journal.entries():
        if entry.get("type") == "answer.job":
            latest[entry["turnId"]] = entry
    return latest


class AnswerJobs:
    def __init__(self, api_key, *, generate=None, concurrency=None):
        self.api_key = api_key
        self.model = os.getenv("EGOASSIST_ANSWER_MODEL", "qwen-flash")
        self.timeout = float(os.getenv("EGOASSIST_ANSWER_TIMEOUT", "20"))
        self.foreground_timeout = float(os.getenv("EGOASSIST_FOREGROUND_ANSWER_TIMEOUT", "45"))
        self.concurrency = concurrency or int(os.getenv("EGOASSIST_ANSWER_CONCURRENCY", "2"))
        if not 1 <= self.timeout <= 300 or not 1 <= self.concurrency <= 8 or not 1 <= self.foreground_timeout <= 300:
            raise ValueError("invalid background answer limits")
        self.generate = generate or self._generate
        self.journals = {}
        self.tasks = {}
        self.closing = False

    @staticmethod
    def transition(journal, row, **changes):
        value = {k: v for k, v in row.items() if k not in {"id", "type", "timestamp", "parentId"}}
        return journal.append("answer.job", {**value, **changes}, parent_id=row.get("id"))

    def register(self, user_id, journal, *, turn_id, question, instructions, context=None, dialogue_job=None, deferred=False):
        self.journals[str(journal.path)] = (user_id, journal)
        if turn_id not in answer_jobs(journal):
            journal.append("answer.job", {"turnId": turn_id, "question": question,
                "instructions": instructions, "context": context or {},
                "dialogueJob": dialogue_job,
                "status": "queued" if deferred else "foreground",
                "model": self.model, "attempt": 0, "createdAt": datetime.now(timezone.utc).isoformat()},
                idempotency_key="answer:" + turn_id)

    def interrupt(self, journal, turn_id):
        row = answer_jobs(journal).get(turn_id)
        if row and row["status"] == "foreground":
            self.transition(journal, row, status="queued", reason="speech-interrupted")

    def foreground_finished(self, journal, turn_id, response_id):
        row = answer_jobs(journal).get(turn_id)
        if row and row["status"] == "foreground":
            self.transition(journal, row, status="completed", channel="generated-audio", responseId=response_id)

    def pause(self, journal, turn_id):
        row = answer_jobs(journal).get(turn_id)
        if row and row["status"] not in {"cancelled", "completed"}:
            self.transition(journal, row, status="paused", reason="user-paused")
            task = self.tasks.get((str(journal.path), turn_id))
            if task:
                task.cancel()

    def resume(self, user_id, journal, turn_id):
        row = answer_jobs(journal).get(turn_id)
        if row and row["status"] != "cancelled":
            self.journals[str(journal.path)] = (user_id, journal)
            self.transition(journal, row, status="foreground", reason="user-resumed",
                            foregroundStartedAt=datetime.now(timezone.utc).isoformat())

    def cancel(self, journal, turn_id, *, reason="user-cancelled"):
        row = answer_jobs(journal).get(turn_id)
        if row is None:
            raise ValueError("unknown answer")
        if row["status"] == "cancelled" or (row["status"] == "completed" and reason == "user-cancelled"):
            return
        self.transition(journal, row, status="cancelled", reason=reason)
        task = self.tasks.get((str(journal.path), turn_id))
        if task:
            task.cancel()

    def retry(self, user_id, journal, turn_id):
        row = answer_jobs(journal).get(turn_id)
        if row is None or row["status"] != "failed":
            raise ValueError("only failed answers can be retried")
        if CaseWorkLedger(journal).turns().get(turn_id, {}).get("supersededBy"):
            raise ValueError("replaced questions cannot be retried")
        self.journals[str(journal.path)] = (user_id, journal)
        self.transition(journal, row, status="queued", error=None, reason="user-retry")

    def recover(self, user_id, journal):
        self.journals[str(journal.path)] = (user_id, journal)
        for row in answer_jobs(journal).values():
            if row["status"] in {"foreground", "running"}:
                self.transition(journal, row, status="queued", reason="server-recovered")

    async def run(self):
        while not self.closing:
            for _, journal in list(self.journals.values()):
                ledger = CaseWorkLedger(journal)
                turns = ledger.turns()
                rows = answer_jobs(journal)
                if not any(row["status"] in {"foreground", "queued", "running"} for row in rows.values()):
                    self.journals.pop(str(journal.path), None)
                    continue
                for row in rows.values():
                    turn = turns.get(row["turnId"], {})
                    if row["status"] not in {"completed", "cancelled"} and (
                        turn.get("supersededBy") or turn.get("status") == "cancelled"
                    ):
                        self.cancel(journal, row["turnId"], reason="question-replaced-or-cancelled")
                    elif row["status"] == "foreground" and (
                        datetime.now(timezone.utc) - datetime.fromisoformat(row.get("foregroundStartedAt") or row["createdAt"])
                    ).total_seconds() >= self.foreground_timeout:
                        self.transition(journal, row, status="queued", reason="foreground-deadline")
                # Give corrections a bounded grace period, including after a disconnect.
                if any(turn.get("status") in {"received", "deferred", "classifying", "ready", "processing"}
                       and (datetime.now(timezone.utc) - datetime.fromisoformat(
                           turn.get("updatedAt") or turn["timestamp"])).total_seconds() < self.foreground_timeout
                       for turn in turns.values()):
                    continue
                for row in rows.values():
                    key = (str(journal.path), row["turnId"])
                    if len(self.tasks) >= self.concurrency:
                        break
                    current = answer_jobs(journal).get(row["turnId"], {})
                    if current.get("status") != "queued" or key in self.tasks:
                        continue
                    task = asyncio.create_task(self._execute(journal, current), name="answer-" + row["turnId"])
                    self.tasks[key] = task
                    task.add_done_callback(lambda finished, key=key: self._finished(key, finished))
            await asyncio.sleep(0.25)

    def _finished(self, key, task):
        self.tasks.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            asyncio.get_running_loop().call_exception_handler({
                "message": "Background answer failed to persist", "exception": task.exception(), "task": task})

    async def _execute(self, journal, row):
        started = time.perf_counter()
        row = self.transition(journal, row, status="running", attempt=row["attempt"] + 1,
                              startedAt=datetime.now(timezone.utc).isoformat(), timeoutSeconds=self.timeout)
        try:
            result = await asyncio.wait_for(self.generate(journal, row), self.timeout)
            current = answer_jobs(journal)[row["turnId"]]
            turn = CaseWorkLedger(journal).turns().get(row["turnId"], {})
            if current["status"] == "running" and not turn.get("supersededBy") and turn.get("status") != "cancelled":
                self.transition(journal, current, **result, status="completed", channel="background-text",
                                durationMs=round((time.perf_counter() - started) * 1000, 3))
                self._finish_dialogue(journal, current)
        except asyncio.CancelledError:
            current = answer_jobs(journal)[row["turnId"]]
            if current["status"] == "running":
                self.transition(journal, current, status="queued" if self.closing else "cancelled",
                                reason="server-shutdown" if self.closing else "cancelled")
            raise
        except Exception as exc:
            current = answer_jobs(journal)[row["turnId"]]
            if current["status"] == "running":
                self.transition(journal, current, status="failed", error=type(exc).__name__ + ": " + str(exc)[:200],
                                durationMs=round((time.perf_counter() - started) * 1000, 3))

    @staticmethod
    def _finish_dialogue(journal, row):
        target = row.get("dialogueJob") or {}
        ledger = CaseWorkLedger(journal)
        intent = ledger.intents().get(target.get("intentId"), {})
        job = ledger.jobs().get(target.get("jobId"), {})
        if (intent.get("kind") != "foreground-dialogue" or intent.get("status") in {"cancelled", "failed"}
                or intent.get("turnIds") != [row["turnId"]]):
            return
        if job.get("status") in {"queued", "running", "waiting-result", "suspended"}:
            ledger.set_job_status(job["jobId"], "completed", detail={"answerTurnId": row["turnId"]})
        if not any(j.get("intentId") == intent["intentId"] and j["status"] not in {"completed", "cancelled", "failed"}
                   for j in ledger.jobs().values()):
            ledger.set_intent_status(intent["intentId"], "completed", detail={"answerTurnId": row["turnId"]})

    async def _generate(self, journal, row):
        if not self.api_key:
            raise ValueError("QWEN_API_KEY is not configured")
        import dashscope
        context = {"groundedContextAtQuestion": row["instructions"],
                   "evidenceAtQuestion": row.get("context", {}),
                   "answeringAt": datetime.now(timezone.utc).isoformat()}
        messages = [{"role": "system", "content": prompt_text("answer_completion/generate")
                     + "\n" + json.dumps(context, ensure_ascii=False)},
                    {"role": "user", "content": row["question"]}]
        request = journal.append("answer.request", {"turnId": row["turnId"], "model": self.model, "messages": messages})
        url = dashscope.base_http_api_url.rstrip("/") + "/services/aigc/text-generation/generation"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=True) as session:
            async with session.post(url, headers={"Authorization": "Bearer " + self.api_key}, json={
                "model": self.model, "input": {"messages": messages},
                "parameters": {"result_format": "message", "temperature": 0, "enable_thinking": False, "max_tokens": 1600},
            }) as response:
                if response.status != 200:
                    raise RuntimeError(f"answer provider HTTP {response.status}")
                payload = await response.json()
        choice = payload["output"]["choices"][0]
        text = choice["message"]["content"]
        if choice.get("finish_reason") not in {None, "stop"} or not isinstance(text, str) or not text.strip():
            raise ValueError("answer is empty or incomplete")
        result = {"turnId": row["turnId"], "text": text, "providerRequestId": payload.get("request_id"),
                  "model": self.model, "usage": payload.get("usage", {})}
        entry = journal.append("answer.result", result, parent_id=request["id"])
        return {**result, "resultEventId": entry["id"]}

    async def delete_user(self, user_id):
        paths = {key for key, (owner, _) in self.journals.items() if owner == user_id}
        tasks = [task for (path, _), task in self.tasks.items() if path in paths]
        self.journals = {key: value for key, value in self.journals.items() if key not in paths}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
