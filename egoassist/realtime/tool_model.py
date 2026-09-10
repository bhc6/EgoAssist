"""Native async tool selection for realtime providers without reliable tool events."""
import json
import os
import time

import aiohttp
import dashscope

from egoassist.prompts import prompt_text


class ToolModel:
    def __init__(self, api_key):
        self.api_key = api_key
        self.model = os.getenv("EGOASSIST_TOOL_MODEL", "qwen3-max")
        self.timeout = float(os.getenv("EGOASSIST_TOOL_TIMEOUT", "8"))
        if not 1 <= self.timeout <= 60:
            raise ValueError("EGOASSIST_TOOL_TIMEOUT must be between 1 and 60 seconds")
        self._session = None

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def select(self, instructions, tools, harness, origin):
        messages = [{"role": "system", "content": prompt_text("tool_use/select")},
                    {"role": "user", "content": instructions}]
        request = harness.journal.append("tool.request", {
            "model": self.model, "messages": messages, "tools": tools, "origin": origin,
        })
        started = time.perf_counter()
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=True)
            async with self._session.post(dashscope.base_http_api_url.rstrip("/") + "/services/aigc/text-generation/generation",
                    headers={"Authorization": "Bearer " + self.api_key}, json={"model": self.model,
                        "input": {"messages": messages}, "parameters": {"tools": tools, "tool_choice": "auto",
                            "result_format": "message", "enable_thinking": False, "temperature": 0}}) as response:
                if response.status != 200:
                    raise RuntimeError(f"Tool model HTTP {response.status}")
                payload = await response.json()
            message = payload["output"]["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 4:
                raise ValueError("Tool model returned an invalid call batch")
            for call in calls:
                arguments = json.loads(call["function"]["arguments"])
                if not call.get("id") or not isinstance(arguments, dict):
                    raise ValueError("Invalid tool call identity or arguments")
            harness.journal.append("tool.selection", {"requestEventId": request["id"], "model": self.model,
                "message": message, "usage": payload.get("usage"), "origin": origin,
                "latencyMs": round((time.perf_counter()-started)*1000, 1)})
            return message
        except Exception as exc:
            harness.journal.append("tool.selection.failed", {"requestEventId": request["id"], "model": self.model,
                "error": type(exc).__name__, "origin": origin, "latencyMs": round((time.perf_counter()-started)*1000, 1)})
            raise
