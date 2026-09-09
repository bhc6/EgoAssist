"""Question-specific visual evidence, independent from background scene descriptions."""
from __future__ import annotations

import asyncio
import base64
import os
import time

import aiohttp

from egoassist.prompts import prompt_text


class VisualQuestions:
    def __init__(self, scene):
        self.scene = scene
        self.pending = {}
        self.results = {}
        self.limit = asyncio.Semaphore(2)
        self.timeout = float(os.getenv("EGOASSIST_VISUAL_QUESTION_TIMEOUT", "4"))
        if not 0.1 <= self.timeout <= 10:
            raise ValueError("visual question timeout must be between 0.1 and 10 seconds")
        self.progress = {}

    async def observe(self, turn_id, question, frame):
        if turn_id in self.results:
            return self.results[turn_id]
        task = self.pending.get(turn_id)
        if task is None:
            self.progress[turn_id] = {"turnId": turn_id, "frameId": frame["frameId"], "started": time.monotonic(), "status": "queued"}
            task = asyncio.create_task(self._observe(turn_id, question, frame), name="question-visual-evidence")
            self.pending[turn_id] = task
            self.scene.tasks.add(task)
            task.add_done_callback(self.scene.tasks.discard)
            task.add_done_callback(lambda _: self.pending.pop(turn_id, None))
            task.add_done_callback(lambda _: self.progress.pop(turn_id, None))
        return await asyncio.shield(task)

    async def _observe(self, turn_id, question, frame):
        import dashscope
        started = time.perf_counter()
        model = os.getenv("EGOASSIST_VISUAL_QUESTION_MODEL", "qwen3-vl-flash")
        prompt = prompt_text("scene/question")
        request = self.scene.journal.append("scene.question.request", {
            "turnId": turn_id, "frame": frame, "model": model, "prompt": prompt, "question": question})
        result = {"turnId": turn_id, "frame": frame, "model": model, "scope": "question-specific"}
        try:
            async with asyncio.timeout(self.timeout), self.limit, aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=self.timeout)) as http:
                self.progress[turn_id]["status"] = "waiting-provider"
                async with http.post(dashscope.base_http_api_url.rstrip("/") + "/services/aigc/multimodal-generation/generation",
                    headers={"Authorization": "Bearer " + self.scene.api_key}, json={"model": model,
                    "input": {"messages": [{"role": "system", "content": [{"text": prompt}]},
                        {"role": "user", "content": [{"image": "data:image/jpeg;base64," + base64.b64encode(self.scene.media(frame)).decode()},
                                                       {"text": question}]}]},
                    "parameters": {"temperature": 0, "enable_thinking": False, "max_tokens": 256}}) as response:
                    response.raise_for_status()
                    payload = await response.json()
            choice = payload["output"]["choices"][0]
            content = choice["message"]["content"]
            description = content if isinstance(content, str) else "".join(c.get("text", "") for c in content)
            if not description.strip() or choice.get("finish_reason") not in {None, "stop"}:
                raise ValueError("incomplete visual evidence")
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
