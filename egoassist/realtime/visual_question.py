"""Question-specific visual evidence, independent from background scene descriptions."""
from __future__ import annotations

import asyncio
import json
import os
import time

import aiohttp

from egoassist.prompts import prompt_text
from egoassist.core.settings import integer


class VisualQuestions:
    def __init__(self, scene):
        self.scene = scene
        self.pending = {}
        self.results = {}
        self.limit = asyncio.Semaphore(integer("EGOASSIST_VISUAL_QUESTION_CONCURRENCY", 2, maximum=8))
        self.timeout = float(os.getenv("EGOASSIST_VISUAL_QUESTION_TIMEOUT", "4"))
        if not 0.1 <= self.timeout <= 10:
            raise ValueError("visual question timeout must be between 0.1 and 10 seconds")
        self.progress = {}

    async def observe(self, turn_id, question, frame, *, window=None):
        if turn_id in self.results:
            return self.results[turn_id]
        task = self.pending.get(turn_id)
        if task is None:
            self.progress[turn_id] = {"turnId": turn_id, "frameId": frame["frameId"], "started": time.monotonic(), "status": "queued"}
            task = asyncio.create_task(self._observe(turn_id, question, frame, window), name="question-visual-evidence")
            self.pending[turn_id] = task
            self.scene.tasks.add(task)
            task.add_done_callback(self.scene.tasks.discard)
            task.add_done_callback(lambda _: self.pending.pop(turn_id, None))
            task.add_done_callback(lambda _: self.progress.pop(turn_id, None))
        return await asyncio.shield(task)

    async def cancel(self, turn_id, *, reason):
        task = self.pending.get(turn_id)
        if task and not task.done():
            self.scene.journal.append("scene.question.cancelled", {"turnId": turn_id, "reason": reason})
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _observe(self, turn_id, question, frame, window):
        import dashscope
        started = time.perf_counter()
        model = os.getenv("EGOASSIST_VISUAL_QUESTION_MODEL", "qwen3-vl-flash")
        frames = (window or {}).get('frames') or [frame]
        sequence = len(frames) > 1
        if sequence:
            from .interaction_window import interaction_vision_model
            model = interaction_vision_model(len(frames))
        prompt = prompt_text("scene/interaction_window" if sequence else "scene/question")
        timeout = float(os.getenv('EGOASSIST_INTERACTION_VISION_TIMEOUT', '8')) if sequence else self.timeout
        if not .1 <= timeout <= 10:
            raise ValueError('interaction vision timeout must be between 0.1 and 10 seconds')
        turn = self.scene.turns[turn_id]
        boundary = turn.get('capturedAt', turn['timestamp'])
        dialogue = [{'role': r['message']['role'], 'text': r['message']['content'][:600], 'sourceId': r['id']}
                    for r in self.scene.journal.entries(types={'message'}, tail=12)
                    if r.get('timestamp', '') <= boundary and r.get('status') == 'completed'
                    and r.get('message', {}).get('role') in {'user', 'assistant'}][-6:]
        request = self.scene.journal.append("scene.question.request", {
            "turnId": turn_id, "frame": frame, "frames": frames, "interactionWindow": window,
            "dialogue": dialogue, "model": model, "prompt": prompt, "question": question})
        result = {"turnId": turn_id, "frame": frame, "model": model, "scope": "interaction-sequence" if sequence else "question-specific",
                  "frames": frames, "window": {k:v for k,v in (window or {}).items() if k != 'frames'}}
        try:
            async with asyncio.timeout(timeout), self.limit:
                result["queueMs"] = round((time.perf_counter() - started) * 1000, 1)
                self.progress[turn_id]["status"] = "preparing-media"
                self.progress[turn_id]["queueMs"] = result["queueMs"]
                from .interaction_window import visual_sequence_content
                content, source_labels, sheets = visual_sequence_content(self.scene, frames)
                result['presentation'] = {'sourceLabels': source_labels, 'sheets': sheets,
                                          'format': 'contact-sheets' if sheets else 'individual-frames'}
                self.scene.journal.append('scene.question.media', {'turnId': turn_id, **result['presentation']}, parent_id=request['id'])
                content.append({'text': json.dumps({'window': result['window'], 'recentDialogue': dialogue}, ensure_ascii=False)})
                content.append({'text': question})
                result['mediaPreparationMs'] = round((time.perf_counter() - started) * 1000 - result['queueMs'], 1)
                provider_started = time.perf_counter()
                self.progress[turn_id]['status'] = 'waiting-provider'
                async with self.scene.http().post(dashscope.base_http_api_url.rstrip("/") + "/services/aigc/multimodal-generation/generation",
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    headers={"Authorization": "Bearer " + self.scene.api_key}, json={"model": model,
                    "input": {"messages": [{"role": "system", "content": [{"text": prompt}]},
                        {"role": "user", "content": content}]},
                    "parameters": {"temperature": 0, "enable_thinking": False, "max_tokens": 500 if sequence else 256,
                                   **({'response_format': {'type': 'json_object'}} if sequence else {})}}) as response:
                    response.raise_for_status()
                    payload = await response.json()
                result['providerMs'] = round((time.perf_counter() - provider_started) * 1000, 1)
            choice = payload["output"]["choices"][0]
            content = choice["message"]["content"]
            description = content if isinstance(content, str) else "".join(c.get("text", "") for c in content)
            if not description.strip() or choice.get("finish_reason") not in {None, "stop"}:
                raise ValueError("incomplete visual evidence")
            if sequence:
                from .visual_history import validate_evidence
                facts, uncertainties = validate_evidence(json.loads(description), set(source_labels))
                facts = [{**fact, 'sourceIds': [source_labels[ref] for ref in fact['sourceIds']]} for fact in facts]
                result.update(evidence=facts, uncertainties=uncertainties, rawOutput=description)
                description = '\n'.join(f['description'] for f in facts)
            result.update(status="observed", description=description, requestId=payload.get("request_id"), usage=payload.get("usage"))
        except Exception as exc:
            result.update(status="unavailable", error=type(exc).__name__ + ": " + str(exc)[:150])
        result["latencyMs"] = round((time.perf_counter() - started) * 1000, 1)
        self.scene.journal.append("scene.question.result", result, parent_id=request["id"])
        if result["status"] == "observed":
            self.results[turn_id] = result
        while len(self.results) > 32:
            self.results.pop(next(iter(self.results)))
        return result
