"""Notification adapters behind the product NotificationPort."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

from .contracts import NotificationReceipt, NotificationRequest


class NtfyNotificationAdapter:
    """Submit minimal reminder content to a configured ntfy topic."""

    def __init__(self, topic: str, *, server: str = "https://ntfy.sh", token: str | None = None) -> None:
        self.topic = str(topic).strip()
        self.server = str(server).rstrip("/")
        self.token = str(token).strip() if token else None
        if not self.topic or not self.server.startswith(("http://", "https://")):
            raise ValueError("ntfy requires a topic and HTTP(S) server")

    async def send(self, request: NotificationRequest) -> NotificationReceipt:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload: dict[str, Any] = {
            "topic": self.topic,
            "title": request.title,
            "message": request.body,
            "tags": ["alarm_clock"],
        }
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.server, json=payload, headers=headers) as response:
                    body = await response.json(content_type=None)
                    if response.status >= 400:
                        raise RuntimeError(f"ntfy returned HTTP {response.status}")
        except Exception as exc:
            return NotificationReceipt(
                notification_id=request.notification_id,
                channel="ntfy",
                status="failed",
                error=str(exc),
            )
        return NotificationReceipt(
            notification_id=request.notification_id,
            channel="ntfy",
            status="acknowledged",
            provider_message_id=str(body.get("id", "")) or None,
            acknowledged_at=datetime.now(timezone.utc),
        )
