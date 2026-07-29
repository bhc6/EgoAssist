"""Narrow infrastructure ports. Adapters own external SDKs; domain code owns policy."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from .contracts import (
    ArtifactDescriptor,
    HumanHelpReceipt,
    HumanHelpRequest,
    JobRecord,
    NotificationReceipt,
    NotificationRequest,
    ObservationEvidence,
)


@runtime_checkable
class InteractionTransport(Protocol):
    async def connect(self, session_id: str) -> None: ...
    async def close(self, session_id: str) -> None: ...
    async def events(self, session_id: str) -> AsyncIterator[Mapping[str, Any]]: ...
    async def publish(self, session_id: str, event: Mapping[str, Any]) -> None: ...


@runtime_checkable
class WorkflowPort(Protocol):
    async def start(self, job: JobRecord) -> str: ...
    async def cancel(self, workflow_id: str, *, reason: str) -> None: ...
    async def signal(self, workflow_id: str, name: str, payload: Mapping[str, Any]) -> None: ...
    async def sleep(self, duration: timedelta) -> None: ...


@runtime_checkable
class NotificationPort(Protocol):
    async def send(self, request: NotificationRequest) -> NotificationReceipt: ...


@runtime_checkable
class ArtifactPort(Protocol):
    async def resolve(self, artifact_id: str, *, owner_id: str) -> ArtifactDescriptor: ...
    async def preview(self, artifact: ArtifactDescriptor) -> Mapping[str, Any]: ...


@runtime_checkable
class HumanHelpPort(Protocol):
    async def submit(self, request: HumanHelpRequest) -> HumanHelpReceipt: ...
    async def cancel(self, request_id: str, *, reason: str) -> HumanHelpReceipt: ...


@runtime_checkable
class PerceptionPort(Protocol):
    async def observe(self, frame: bytes, *, context: Mapping[str, Any]) -> ObservationEvidence: ...
