"""Configured webhook connector for consented human-help requests."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .contracts import HumanHelpReceipt, HumanHelpRequest


class WebhookHumanHelpAdapter:
    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 8.0,
        session_factory=aiohttp.ClientSession,
    ) -> None:
        normalized = str(endpoint).strip()
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("human-help webhook must use http or https")
        if timeout_seconds <= 0:
            raise ValueError("human-help timeout must be positive")
        self.endpoint = normalized
        self.token = str(token).strip() if token else None
        self.timeout_seconds = float(timeout_seconds)
        self._session_factory = session_factory

    @classmethod
    def from_env(cls) -> "WebhookHumanHelpAdapter | None":
        endpoint = str(os.getenv("EGOASSIST_HUMAN_HELP_WEBHOOK", "")).strip()
        if not endpoint:
            return None
        return cls(
            endpoint,
            token=os.getenv("EGOASSIST_HUMAN_HELP_TOKEN"),
            timeout_seconds=float(os.getenv("EGOASSIST_HUMAN_HELP_TIMEOUT", "8")),
        )

    def public_summary(self) -> dict[str, Any]:
        parsed = urlsplit(self.endpoint)
        return {
            "configured": True,
            "connector": "webhook",
            "endpointOrigin": f"{parsed.scheme}://{parsed.netloc}",
            "timeoutSeconds": self.timeout_seconds,
            "authentication": "bearer" if self.token else "none",
        }

    async def submit(self, request: HumanHelpRequest) -> HumanHelpReceipt:
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": request.idempotency_key,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with self._session_factory(timeout=timeout) as session:
                async with session.post(
                    self.endpoint,
                    json=request.model_dump(mode="json"),
                    headers=headers,
                ) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = {}
                    if not 200 <= response.status < 300:
                        return HumanHelpReceipt(
                            request_id=request.request_id,
                            connector="webhook",
                            status="failed",
                            error=f"webhook returned HTTP {response.status}",
                        )
        except asyncio.TimeoutError:
            return HumanHelpReceipt(
                request_id=request.request_id,
                connector="webhook",
                status="timed-out",
                error=f"webhook exceeded {self.timeout_seconds:g}s timeout",
            )
        except aiohttp.ClientError as exc:
            return HumanHelpReceipt(
                request_id=request.request_id,
                connector="webhook",
                status="failed",
                error=str(exc)[:240],
            )
        raw_status = str(payload.get("status", "submitted")).strip().casefold()
        status = raw_status if raw_status in {"submitted", "acknowledged", "resolved"} else "submitted"
        return HumanHelpReceipt(
            request_id=request.request_id,
            connector="webhook",
            status=status,
            external_request_id=str(
                payload.get("requestId") or payload.get("id") or ""
            ).strip() or None,
            message=str(payload.get("message") or "").strip() or None,
        )

    async def cancel(self, request_id: str, *, reason: str) -> HumanHelpReceipt:
        return HumanHelpReceipt(
            request_id=str(request_id),
            connector="webhook",
            status="failed",
            error=f"Connector cancellation endpoint is not configured: {reason}",
        )
