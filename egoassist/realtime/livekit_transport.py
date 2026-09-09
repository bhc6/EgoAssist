"""LiveKit media adapter for the EgoAssist interaction transport port.

LiveKit owns rooms, track delivery, congestion control, and reconnect. EgoAssist
continues to own turn persistence, intent attribution, memory, and output policy.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from io import BytesIO
import json
import os
from typing import Any

from PIL import Image
from livekit import api, rtc


LIVEKIT_DATA_TOPIC = "egoassist.control.v1"


@dataclass(frozen=True)
class LiveKitSettings:
    url: str
    api_key: str
    api_secret: str
    token_ttl_seconds: int = 900

    @classmethod
    def from_env(cls) -> "LiveKitSettings | None":
        url = str(os.getenv("LIVEKIT_URL", "")).strip()
        api_key = str(os.getenv("LIVEKIT_API_KEY", "")).strip()
        api_secret = str(os.getenv("LIVEKIT_API_SECRET", "")).strip()
        if not (url and api_key and api_secret):
            return None
        return cls(url=url, api_key=api_key, api_secret=api_secret)

    def public_summary(self) -> dict[str, Any]:
        return {
            "configured": True,
            "url": self.url,
            "tokenTtlSeconds": self.token_ttl_seconds,
        }


@dataclass
class _RoomSession:
    room: Any
    queue: asyncio.Queue[dict[str, Any] | None]
    tasks: set[asyncio.Task] = field(default_factory=set)
    output_source: Any | None = None
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LiveKitInteractionTransport:
    """Normalize LiveKit tracks and data packets into interaction events."""

    def __init__(
        self,
        settings: LiveKitSettings,
        *,
        room_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._room_factory = room_factory or rtc.Room
        self._sessions: dict[str, _RoomSession] = {}

    @property
    def healthy(self) -> bool:
        return bool(self.settings.url and self.settings.api_key and self.settings.api_secret)

    @property
    def connected_sessions(self) -> tuple[str, ...]:
        return tuple(sorted(self._sessions))

    def issue_browser_token(self, session_id: str, participant_id: str) -> str:
        room_name = self.room_name(session_id)
        grants = api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            can_publish_sources=["microphone", "camera"],
        )
        return (
            api.AccessToken(self.settings.api_key, self.settings.api_secret)
            .with_identity(str(participant_id))
            .with_name("EgoAssist participant")
            .with_ttl(timedelta(seconds=self.settings.token_ttl_seconds))
            .with_grants(grants)
            .to_jwt()
        )

    @staticmethod
    def room_name(session_id: str) -> str:
        # Session IDs are already validated by SessionJournal. A prefix prevents
        # accidental collision with rooms owned by another application.
        return f"egoassist-{session_id}"

    def _issue_backend_token(self, session_id: str) -> str:
        room_name = self.room_name(session_id)
        grants = api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            hidden=True,
        )
        return (
            api.AccessToken(self.settings.api_key, self.settings.api_secret)
            .with_identity(f"egoassist-backend-{session_id}")
            .with_name("EgoAssist interaction core")
            .with_ttl(timedelta(seconds=self.settings.token_ttl_seconds))
            .with_grants(grants)
            .to_jwt()
        )

    async def connect(self, session_id: str) -> None:
        if session_id in self._sessions:
            return
        room = self._room_factory()
        session = _RoomSession(room=room, queue=asyncio.Queue(maxsize=256))
        self._sessions[session_id] = session

        def spawn(coroutine) -> None:
            task = asyncio.create_task(coroutine)
            session.tasks.add(task)
            task.add_done_callback(session.tasks.discard)

        def on_track(track, _publication, participant) -> None:
            identity = str(getattr(participant, "identity", ""))
            if identity.startswith("egoassist-backend-"):
                return
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                spawn(self._consume_audio(session_id, session, track, identity))
            elif track.kind == rtc.TrackKind.KIND_VIDEO:
                spawn(self._consume_video(session_id, session, track, identity))

        def on_data(packet) -> None:
            if str(getattr(packet, "topic", "")) != LIVEKIT_DATA_TOPIC:
                return
            participant = getattr(packet, "participant", None)
            identity = str(getattr(participant, "identity", ""))
            if identity.startswith("egoassist-backend-"):
                return
            try:
                payload = json.loads(bytes(packet.data).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                return
            if isinstance(payload, dict):
                self._put_event(session, {**payload, "transport": "livekit", "participantId": identity})

        def on_disconnected(*_args) -> None:
            self._put_event(session, {
                "type": "transport_status",
                "status": "disconnected",
                "transport": "livekit",
            })

        room.on("track_subscribed", on_track)
        room.on("data_received", on_data)
        room.on("disconnected", on_disconnected)
        try:
            await room.connect(self.settings.url, self._issue_backend_token(session_id))
        except BaseException:
            self._sessions.pop(session_id, None)
            raise
        self._put_event(session, {
            "type": "transport_status",
            "status": "connected",
            "transport": "livekit",
        })

    async def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        for task in tuple(session.tasks):
            task.cancel()
        if session.tasks:
            await asyncio.gather(*tuple(session.tasks), return_exceptions=True)
        await session.room.disconnect()
        self._put_event(session, None)

    async def close_all(self) -> None:
        await asyncio.gather(*(self.close(item) for item in tuple(self._sessions)))

    async def events(self, session_id: str) -> AsyncIterator[Mapping[str, Any]]:
        session = self._sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"LiveKit session is not connected: {session_id}")
        while True:
            event = await session.queue.get()
            if event is None:
                return
            yield event

    async def publish(self, session_id: str, event: Mapping[str, Any]) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"LiveKit session is not connected: {session_id}")
        payload = dict(event)
        if payload.get("type") == "audio" and payload.get("audio"):
            await self._publish_audio(session, str(payload["audio"]))
            return
        if payload.get("type") in {"interrupted", "playback_stop"} and session.output_source is not None:
            session.output_source.clear_queue()
        await session.room.local_participant.publish_data(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            reliable=True,
            topic=LIVEKIT_DATA_TOPIC,
        )

    async def _ensure_output_source(self, session: _RoomSession):
        if session.output_source is not None:
            return session.output_source
        async with session.output_lock:
            if session.output_source is not None:
                return session.output_source
            source = rtc.AudioSource(24_000, 1, queue_size_ms=2_000)
            track = rtc.LocalAudioTrack.create_audio_track("egoassist-speech", source)
            await session.room.local_participant.publish_track(track)
            session.output_source = source
            return source

    async def _publish_audio(self, session: _RoomSession, audio_b64: str) -> None:
        raw = base64.b64decode(audio_b64, validate=True)
        if len(raw) % 2:
            raise ValueError("PCM16 payload must contain complete samples")
        source = await self._ensure_output_source(session)
        frame = rtc.AudioFrame(raw, 24_000, 1, len(raw) // 2)
        await source.capture_frame(frame)

    async def _consume_audio(self, session_id: str, session: _RoomSession, track, participant_id: str) -> None:
        stream = rtc.AudioStream(track, capacity=100, sample_rate=16_000, num_channels=1, frame_size_ms=20)
        try:
            async for item in stream:
                raw = item.frame.data.cast("B").tobytes()
                self._put_event(session, {
                    "type": "audio",
                    "audio": base64.b64encode(raw).decode("ascii"),
                    "sampleRate": item.frame.sample_rate,
                    "channels": item.frame.num_channels,
                    "participantId": participant_id,
                    "transport": "livekit",
                })
        finally:
            await stream.aclose()

    async def _consume_video(self, session_id: str, session: _RoomSession, track, participant_id: str) -> None:
        stream = rtc.VideoStream(track, capacity=1, format=rtc.VideoBufferType.RGB24)
        try:
            async for item in stream:
                frame = item.frame
                image = Image.frombytes("RGB", (frame.width, frame.height), frame.data.tobytes())
                output = BytesIO()
                image.save(output, format="JPEG", quality=72)
                self._put_event(session, {
                    "type": "video",
                    "image": base64.b64encode(output.getvalue()).decode("ascii"),
                    "capturedAtUs": int(item.timestamp_us),
                    "participantId": participant_id,
                    "transport": "livekit",
                })
        finally:
            await stream.aclose()

    @staticmethod
    def _put_event(session: _RoomSession, event: dict[str, Any] | None) -> None:
        try:
            session.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Keep fresh realtime media. Emit an explicit gap before the newest
            # frame so downstream memory/safety code never assumes continuity.
            for _ in range(2):
                try:
                    session.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            gap = {
                "type": "media_gap",
                "media": str((event or {}).get("type", "unknown")),
                "reason": "livekit-adapter-backpressure",
                "transport": "livekit",
            }
            try:
                session.queue.put_nowait(gap)
                session.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


def livekit_capability(settings: LiveKitSettings | None, *, connected: bool = False) -> dict[str, Any]:
    if settings is None:
        return {
            "status": "unavailable",
            "reason": "LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are required.",
        }
    return {
        "status": "available" if connected else "configured",
        "reason": (
            "LiveKit room transport is connected."
            if connected else "LiveKit is configured; WebSocket fallback remains active until room connection."
        ),
        **settings.public_summary(),
    }
