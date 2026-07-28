"""Optional structured observation adapter for external camera pipelines."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any


class StructuredPerceptionAdapter:
    """Map a frame to auditable checkpoint evidence outside the Realtime bridge."""

    def __init__(self, mode: str = "manual", model: str | None = None, endpoint: str | None = None, api_key: str | None = None) -> None:
        self.mode = mode
        self.model = model or os.getenv("EGOASSIST_PERCEPTION_MODEL", "gpt-4o-mini")
        self.endpoint = endpoint or os.getenv("EGOASSIST_PERCEPTION_ENDPOINT", "https://api.openai.com/v1/chat/completions")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        if self.mode == "openai" and not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for openai perception mode")

    def analyze(self, image_data_url: str, current_checkpoint: str, allowed_checkpoints: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        if self.mode != "openai":
            return {
                "step": "unknown", "completed": False, "confidence": 0.0,
                "summary": "摄像头已连接；人工模式不对画面作模型推断。",
                "hazards": [], "source": "camera/manual",
            }
        if not re.match(r"^data:image/(jpeg|png|webp);base64,", image_data_url):
            raise ValueError("frame must be a base64 image data URL")
        if len(image_data_url) > 2_000_000:
            raise ValueError("frame is too large")
        allowed = list(dict.fromkeys([*allowed_checkpoints, "unknown"]))
        if allowed == ["unknown"]:
            raise ValueError("allowed checkpoints are required for model perception")
        prompt = (
            "Conservatively map visible egocentric evidence to an optional checkpoint hypothesis. "
            f"The current hypothesis is {current_checkpoint}. Return JSON only with: "
            f"step one of {json.dumps(allowed)}; completed boolean; confidence 0..1; "
            "summary short Chinese text; hazards array of {type,message,confidence}. "
            "Do not infer the user's goal or invisible completion. Report only clearly visible evidence."
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 250,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url, "detail": "low"}},
            ]}],
        }
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
        observation = json.loads(result["choices"][0]["message"]["content"])
        observation["source"] = f"camera/{self.model}"
        return observation
