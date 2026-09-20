"""Finish interrupted answers without replaying user controls or tool actions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import time

from egoassist.prompts import prompt_text
from egoassist.realtime.work_ledger import CaseWorkLedger
from egoassist.core.events import latest


def answer_jobs(journal):
    return latest(journal, "answer.job", "turnId")


def answer_job_summaries(journal):
    """Router targets need identity and status, not copied media/instructions."""
    def reduce(rows, entry):
        if entry.get('type') == 'answer.job' and entry.get('turnId'):
            rows[entry['turnId']] = {key: entry.get(key) for key in ('turnId', 'question', 'status')}
    return journal.project('answer-routing-summaries', dict, reduce)


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
        from egoassist.product.answer_service import AnswerService
        self.answer_service = AnswerService(api_key, model=self.model, timeout=self.timeout)
        self.journals = {}
        self.tasks = {}
        self.closing = False
        from egoassist.core.settings import number
        self.correction_grace = number("EGOASSIST_ANSWER_CORRECTION_GRACE", 2, maximum=30)

    @staticmethod
    def transition(journal, row, **changes):
        value = {k: v for k, v in row.items() if k not in {"id", "type", "timestamp", "parentId"}}
        return journal.append("answer.job", {**value, **changes}, parent_id=row.get("id"),
                              required_event_ids=(row['id'],))

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

    def foreground_finished(self, journal, turn_id, response_id, *, audio_available=None):
        row = answer_jobs(journal).get(turn_id)
        if row and row["status"] == "foreground":
            self.transition(journal, row, status="completed",
                            channel="generated-text" if audio_available is False else "generated-audio", responseId=response_id)

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
                unsettled = self._unsettled(turns)
                for row in rows.values():
                    key = (str(journal.path), row["turnId"])
                    if len(self.tasks) >= self.concurrency:
                        break
                    current = answer_jobs(journal).get(row["turnId"], {})
                    if current.get("status") != "queued" or key in self.tasks:
                        continue
                    queued_age = (datetime.now(timezone.utc) - datetime.fromisoformat(current["timestamp"])).total_seconds()
                    if unsettled and (current.get("preparedResult") or queued_age < self.correction_grace):
                        continue
                    task = asyncio.create_task(self._execute(journal, current), name="answer-" + row["turnId"])
                    self.tasks[key] = task
                    task.add_done_callback(lambda finished, key=key: self._finished(key, finished))
            await asyncio.sleep(0.25)

    def _unsettled(self, turns):
        return any(turn.get("status") in {"received", "deferred", "classifying", "ready", "processing"}
                   and (datetime.now(timezone.utc) - datetime.fromisoformat(
                       turn.get("updatedAt") or turn["timestamp"])).total_seconds() < self.foreground_timeout
                   for turn in turns.values())

    def _finished(self, key, task):
        self.tasks.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            asyncio.get_running_loop().call_exception_handler({
                "message": "Background answer failed to persist", "exception": task.exception(), "task": task})

    async def _execute(self, journal, row):
        started = time.perf_counter()
        prepared = row.get("preparedResult")
        try:
            self._check_snapshot(journal, row)
            row = self.transition(journal, row, status="running", attempt=row["attempt"] + (0 if prepared else 1),
                                  startedAt=datetime.now(timezone.utc).isoformat(), timeoutSeconds=self.timeout)
            result = prepared or await asyncio.wait_for(self.generate(journal, row), self.timeout)
            self._check_snapshot(journal, row)
            duration_ms = row.get("generationMs", 0) if prepared else round((time.perf_counter() - started) * 1000, 3)
            current = answer_jobs(journal)[row["turnId"]]
            turn = CaseWorkLedger(journal).turns().get(row["turnId"], {})
            if current["status"] == "running" and not turn.get("supersededBy") and turn.get("status") != "cancelled":
                if self._unsettled(CaseWorkLedger(journal).turns()):
                    self.transition(journal, current, status="queued", preparedResult=result,
                                    generationMs=duration_ms, phase="waiting-corrections", reason="result-ready-awaiting-turn-decisions")
                else:
                    self.transition(journal, current, **result, status="completed", channel="background-text",
                                    preparedResult=None, phase="completed",
                                    durationMs=duration_ms, generationMs=duration_ms)
                    self._finish_dialogue(journal, current)
        except asyncio.CancelledError:
            current = answer_jobs(journal).get(row["turnId"], {}) if journal.path.exists() else {}
            if current.get("status") == "running":
                self.transition(journal, current, status="queued" if self.closing else "cancelled",
                                reason="server-shutdown" if self.closing else "cancelled")
            raise
        except Exception as exc:
            current = answer_jobs(journal).get(row["turnId"], {}) if journal.path.exists() else {}
            if current.get("status") in {"running", "queued"}:
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

    @staticmethod
    def _check_snapshot(journal, row):
        packet = (row.get('context') or {}).get('evidencePackage')
        if packet:
            from egoassist.memory.source_index import SourceIndex
            SourceIndex(journal.path.parent.parent).snapshot(packet['snapshotId'], journal.session_id, packet['question'])

    async def _generate(self, journal, row):
        if not self.api_key:
            raise ValueError("QWEN_API_KEY is not configured")
        context = {"groundedContextAtQuestion": row["instructions"],
                   "evidenceAtQuestion": row.get("context", {}),
                   "answeringAt": datetime.now(timezone.utc).isoformat()}
        scene = (row.get("context") or {}).get("sharedScene") or {}
        if scene:
            from egoassist.realtime.interaction_window import answer_scene_context
            context['evidenceAtQuestion'] = {**context['evidenceAtQuestion'], 'sharedScene': answer_scene_context(scene)}
        packet = (row.get('context') or {}).get('evidencePackage')
        if packet:
            from egoassist.product.answer_service import answer_context
            from egoassist.memory.source_index import SourceIndex
            SourceIndex(journal.path.parent.parent).snapshot(packet['snapshotId'], journal.session_id, packet['question'])
            context['evidenceAtQuestion'] = {**context['evidenceAtQuestion'], 'evidencePackage': answer_context(packet)}
        messages = [{"role": "system", "content": prompt_text("answer_completion/generate")
                     + "\n" + json.dumps(context, ensure_ascii=False)},
                    {"role": "user", "content": row["question"]}]
        frame = (scene.get("visual") or {}).get("frame")
        frames = (scene.get('interactionWindow') or {}).get('frames') or ([frame] if frame else [])
        model = self.model
        endpoint = "/services/aigc/text-generation/generation"
        audit_messages = messages
        if frames:
            from egoassist.realtime.scene import SharedScene
            from egoassist.realtime.interaction_window import interaction_vision_model, visual_sequence_content
            media = SharedScene(journal, self.api_key)
            content, labels, sheets = visual_sequence_content(media, frames)
            content.append({'text': json.dumps({'imageSourceIds': labels})})
            content.append({'text': row['question']})
            model = (interaction_vision_model(len(frames)) if len(frames) > 1
                     else os.getenv("EGOASSIST_SCENE_MODEL", "qwen3-vl-flash"))
            endpoint = "/services/aigc/multimodal-generation/generation"
            audit_messages = messages + [{"role": "user", "content": [{"mediaEvidence": ref} for ref in frames]}]
            audit_messages.append({'role': 'user', 'content': {'imageSourceIds': labels, 'sheets': sheets}})
            messages = [{"role": "system", "content": [{"text": messages[0]["content"]}]},
                        {"role": "user", "content": content}]
        request = journal.append("answer.request", {"turnId": row["turnId"], "model": model,
            "answerJobEventId": row['id'], "evidencePackage": packet, "messages": audit_messages},
            required_event_ids=(row['id'],))
        payload = await self.answer_service.complete(messages, model=model, endpoint=endpoint,
            timeout=self.timeout, parameters={"result_format": "message", "temperature": 0,
                "enable_thinking": False, "max_tokens": 1600})
        choice = payload["output"]["choices"][0]
        if packet:
            SourceIndex(journal.path.parent.parent).snapshot(packet['snapshotId'], journal.session_id, packet['question'])
        text = choice["message"]["content"]
        if isinstance(text, list):
            text = "".join(item.get("text", "") for item in text)
        if choice.get("finish_reason") not in {None, "stop"} or not isinstance(text, str) or not text.strip():
            raise ValueError("answer is empty or incomplete")
        result = {"turnId": row["turnId"], "text": text, "providerRequestId": payload.get("request_id"),
                  "model": model, "usage": payload.get("usage", {}), "frameEvidence": frame, "frameSequenceEvidence": frames}
        entry = journal.append("answer.result", {**result, 'requestEventId': request['id']}, parent_id=request["id"],
                               required_event_ids=(row['id'], request['id']))
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
        await self.answer_service.close()
