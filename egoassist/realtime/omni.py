"""Qwen Omni realtime bridge for browser audio/video streams."""

from __future__ import annotations

from egoassist.prompts import prompt_text
from egoassist.core.provider_errors import access_failure

import asyncio
import base64
from collections import deque
from copy import deepcopy
import difflib
import ipaddress
import inspect
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

import requests

from .tools import tool_result_succeeded
from egoassist.core.settings import integer
from .ambient import AMBIENT_SCHEMA_VERSION, NONE_OBSERVED, UNKNOWN_LOCATION
from .perception import (
    frame_prompt,
    parse_ambient_hypothesis,
    parse_perception_hypothesis,
)

if TYPE_CHECKING:
    from .context_harness import ContextHarness


_DNS_LOCK = threading.Lock()
_DASHSCOPE_HOST = "dashscope.aliyuncs.com"
LOGGER = logging.getLogger(__name__)


def _bounded_timeout(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _provider_error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    return str(error.get("message", "") if isinstance(error, dict) else error)


def _is_media_order_error(payload: dict[str, Any]) -> bool:
    message = _provider_error_message(payload).casefold()
    return "append image before append audio" in message


@dataclass(frozen=True)
class _ResponseRequest:
    instructions: str
    priority: int
    sequence: int
    speech_policy: str = "immediate"
    reason: str = "unspecified"
    requested_at: float = 0.0
    max_protection_ms: int = 2500
    output_mode: str = "audio"
    purpose: str = "conversation"
    origin: dict[str, Any] | None = None

    @property
    def correlation(self) -> dict:
        return {key: value for key, value in (self.origin or {}).items()
                if key in {"turnId", "generation", "sessionId", "intentId", "jobId", "resultId", "background", "observationReviewId", "contextId"}}


def _is_fake_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in ipaddress.ip_network("198.18.0.0/15")
    except ValueError:
        return False


def _is_active_response_conflict(payload: dict[str, Any]) -> bool:
    error = payload.get("error", {})
    message = error.get("message", payload) if isinstance(error, dict) else payload
    return "conversation already has an active response" in str(message).casefold()


def _is_no_active_response(payload: dict[str, Any]) -> bool:
    error = payload.get("error", {})
    message = error.get("message", payload) if isinstance(error, dict) else payload
    normalized = str(message).casefold()
    return "none active response" in normalized or "no active response" in normalized


def _is_response_idle_timeout(payload: dict[str, Any]) -> bool:
    error = payload.get("error", {})
    if not isinstance(error, dict):
        error = {"message": error}
    code = str(error.get("code", "")).casefold().replace("_", " ")
    message = str(error.get("message", payload)).casefold()
    return "response idle timeout" in code or (
        "no response was generated" in message and "session was closed" in message
    )


def _realtime_public_ips(host: str = _DASHSCOPE_HOST) -> list[str]:
    override = os.getenv("DASHSCOPE_REALTIME_IPS", "")
    if override:
        return [item.strip() for item in override.split(",") if item.strip()]
    try:
        system_ips = sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except (OSError, socket.gaierror) as exc:
        raise ConnectionError(
            f"DashScope DNS lookup failed for {host}; configure DASHSCOPE_REALTIME_IPS or a working resolver"
        ) from exc
    if system_ips and not all(_is_fake_ip(item) for item in system_ips):
        # The SDK's WebSocket dial has no Happy Eyeballs and only waits 5s.
        # Prefer current DNS IPv4 answers on dual-stack hosts; preserve SNI.
        ipv4 = [item for item in system_ips if ":" not in item and not _is_fake_ip(item)]
        if ipv4 and any(":" in item for item in system_ips):
            return ipv4
        return []
    resolver_urls = [
        item.strip() for item in os.getenv(
            "DASHSCOPE_DOH_URLS",
            "https://cloudflare-dns.com/dns-query,https://dns.google/resolve,https://dns.alidns.com/resolve",
        ).split(",") if item.strip()
    ]
    errors: list[str] = []
    timeout = _bounded_timeout("EGOASSIST_DNS_TIMEOUT_SECONDS", 3.0, 0.5, 10.0)
    for resolver_url in resolver_urls:
        try:
            headers = {"Accept": "application/dns-json"} if "cloudflare-dns.com" in resolver_url else {}
            response = requests.get(
                resolver_url, params={"name": host, "type": "A"},
                headers=headers, timeout=timeout,
            )
            response.raise_for_status()
            addresses = list(dict.fromkeys(
                str(item["data"]) for item in response.json().get("Answer", [])
                if item.get("type") == 1 and not _is_fake_ip(str(item.get("data", "")))
            ))
            if addresses:
                return addresses
            errors.append(f"{resolver_url}: no public A record")
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{resolver_url}: {type(exc).__name__}")
    detail = "; ".join(errors[-3:]) or "no resolver configured"
    raise ConnectionError(
        f"DashScope DNS is unavailable for {host}. Set DASHSCOPE_REALTIME_IPS to approved provider IPs "
        f"or allow one configured DoH endpoint ({detail})"
    )


def _connect_with_resolved_ip(conversation, host: str = _DASHSCOPE_HOST) -> None:
    if getattr(conversation, "uses_http_pipeline", False):
        conversation.connect()
        return
    public_ips = _realtime_public_ips(host)
    if not public_ips:
        conversation.connect()
        return
    original = socket.getaddrinfo
    errors: list[str] = []
    for public_ip in public_ips:
        def targeted_getaddrinfo(name, port, family=0, type=0, proto=0, flags=0):
            target = public_ip if str(name).casefold() == host.casefold() else name
            return original(target, port, family, type, proto, flags)

        with _DNS_LOCK:
            socket.getaddrinfo = targeted_getaddrinfo
            try:
                conversation.connect()
                return
            except Exception as exc:
                if getattr(conversation, "_egoassist_access_failure", None):
                    raise conversation._egoassist_access_failure from exc
                errors.append(f"{public_ip}: {exc}")
                if getattr(conversation, "ws", None):
                    conversation.close()
            finally:
                socket.getaddrinfo = original
    raise ConnectionError("DashScope realtime connection failed via public DNS: " + "; ".join(errors))


def _observe_access_errors(conversation, on_failure):
    # The SDK logs WebSocket errors without forwarding them to its callback.
    original = getattr(conversation, "_on_error", None)
    if original is not None:
        def on_error(ws, error):
            failure = access_failure(error)
            if failure is not None:
                conversation._egoassist_access_failure = failure
                on_failure(failure)
            original(ws, error)
        conversation._on_error = on_error
    return conversation


class _OmniPerceptionLane:
    """Isolated text-only visual gate; it never receives microphone audio."""

    _SILENT_PCM16 = base64.b64encode(bytes(640)).decode("ascii")
    _GATE_INSTRUCTIONS = (
        prompt_text('omni/_GATE_INSTRUCTIONS.1')
    )

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        loop: asyncio.AbstractEventLoop,
        conversation_cls,
        callback_cls,
        modalities,
        on_decision: Callable[[dict[str, Any]], Any],
        trace: Callable[..., Any],
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.loop = loop
        self._conversation_cls = conversation_cls
        self._callback_cls = callback_cls
        self.modalities = modalities
        self.on_decision = on_decision
        self.trace = trace
        self.conversation = None
        self.connected = False
        self.closed = False
        self.active = False
        self.pending_frame: tuple[str, dict[str, Any] | None] | None = None
        self.active_context: dict[str, Any] | None = None
        self._buffer = ""
        self._generation = 0
        self._connect_lock = asyncio.Lock()
        self._reconnect_scheduled = False
        self._blocked_failure = None
        self._request_started = None
        from egoassist.core.settings import perception_settings
        self.settings = perception_settings()
        self._response_starts = {}
        self._suspended = False

    def _new_conversation(self, generation: int):
        lane = self

        class Callback(self._callback_cls):
            def on_open(self):
                return None

            def on_close(self, close_status_code=None, close_msg=None):
                if generation == lane._generation and not lane.closed:
                    lane.connected = False
                    failure = access_failure(close_msg)
                    if failure is not None:
                        lane._block_provider(failure)
                    lane._schedule_reconnect("provider-closed")

            def on_event(self, response):
                if generation != lane._generation or lane.closed:
                    return
                event_type = str(response.get("type", ""))
                if event_type == "pipeline.metrics":
                    lane.trace("provider.pipeline.metrics", {**response, "lane": "perception"})
                elif event_type == "pipeline.error":
                    lane.trace("provider.pipeline.error", {**response, "lane": "perception"})
                if event_type == "response.created":
                    lane._response_starts[lane._response_id(response)] = lane._request_started
                    lane.trace("perception.response.created", {"lane": "perception"}, response_id=lane._response_id(response))
                elif event_type in {"response.text.delta", "response.audio_transcript.delta"}:
                    lane._buffer += str(response.get("delta", ""))
                elif event_type in {"response.text.done", "response.audio_transcript.done"}:
                    # The text event is not terminal; the previous response.done
                    # must not consume a subsequently dispatched frame.
                    lane._buffer = str(response.get("text") or response.get("transcript") or lane._buffer)
                elif event_type == "response.done":
                    response_id = lane._response_id(response)
                    started = lane._response_starts.pop(response_id, None)
                    lane.trace("perception.response.finished", {
                        "lane": "perception", "model": lane.model,
                        "usage": OmniBrowserBridge._response_usage(response),
                        "providerStatus": (response.get("response") or {}).get("status"),
                        "latencyMs": round((time.monotonic() - started) * 1000, 1) if started is not None else None,
                    }, response_id=response_id)
                    if lane.active:
                        lane._finish(lane._buffer)
                elif event_type in {"response.failed", "response.cancelled"}:
                    lane._response_starts.pop(lane._response_id(response), None)
                    lane._finish("")
                elif event_type == "error":
                    failure = access_failure(response)
                    if failure is not None:
                        lane._block_provider(failure)
                    elif _is_response_idle_timeout(response):
                        lane.connected = False
                        lane.trace("provider.session.idle_timeout", {"lane": "perception"})
                        lane._schedule_reconnect("response-idle-timeout")
                    else:
                        lane.trace("perception.response.error", {"error": str(response)})
                        lane._finish("")

        conversation = self._conversation_cls(
            model=self.model,
            callback=Callback(),
            api_key=self.api_key,
            url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
        )
        return _observe_access_errors(conversation, lambda failure: lane._block_provider(failure)
                                      if generation == lane._generation and not lane.closed else None)

    def _block_provider(self, failure):
        if self._blocked_failure is not None:
            return
        self._blocked_failure = failure
        self.connected = False
        self.active = False
        self.pending_frame = None
        self.active_context = None
        self.trace("provider.account.blocked", {"lane": "perception", **failure.public()})

    @staticmethod
    def _response_id(payload: dict[str, Any]) -> str | None:
        nested = payload.get("response", {})
        value = payload.get("response_id") or (nested.get("id") if isinstance(nested, dict) else None)
        return str(value) if value else None

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._blocked_failure is not None:
                raise self._blocked_failure
            if self.closed or self.connected or self._suspended:
                return
            self._generation += 1
            generation = self._generation
            conversation = self._new_conversation(generation)
            self.conversation = conversation
            await asyncio.to_thread(_connect_with_resolved_ip, conversation)
            if self._blocked_failure is not None:
                await asyncio.to_thread(conversation.close)
                raise self._blocked_failure
            if generation != self._generation or self.closed or self._suspended:
                await asyncio.to_thread(conversation.close)
                return
            conversation.update_session(
                output_modalities=[self.modalities.TEXT],
                voice=self.voice,
                instructions=self._GATE_INSTRUCTIONS,
                enable_input_audio_transcription=False,
                enable_turn_detection=False,
            )
            self.connected = True
            self.trace("provider.session.ready", {"lane": "perception", "generation": generation})
        if self.pending_frame and not self.active:
            self._dispatch_pending()

    def request(self, image_b64: str, context: dict[str, Any] | None = None) -> bool:
        if not image_b64 or self.closed or self._blocked_failure is not None:
            return False
        self._suspended = False
        if self.pending_frame:
            previous = self.pending_frame[1] or {}
            self.trace("perception.frame.coalesced", {"frameId": previous.get("frame_id"),
                       "contextId": previous.get("context_id"), "reason": "newer-pending-frame"})
        self.pending_frame = (image_b64, deepcopy(context) if context else None)
        if not self.connected:
            self._schedule_reconnect("perception-demand")
            return False
        if self.active:
            return False
        return self._dispatch_pending()

    def _dispatch_pending(self) -> bool:
        if self._suspended or not self.connected or self.active or not self.pending_frame or not self.conversation:
            return False
        frame, context = self.pending_frame
        self.pending_frame = None
        self.active = True
        self.active_context = context
        self._buffer = ""
        self._request_started = time.monotonic()
        try:
            instructions = frame_prompt(context) if context else self._GATE_INSTRUCTIONS
            self.conversation.update_session(output_modalities=[self.modalities.TEXT], voice=self.voice,
                instructions=instructions, enable_input_audio_transcription=False, enable_turn_detection=False)
            # Qwen Omni requires audio to precede an image. This lane uses a
            # deterministic silent ordering token, never user microphone data.
            if not getattr(self.conversation, "uses_http_pipeline", False):
                self.conversation.append_audio(self._SILENT_PCM16)
            self.conversation.append_video(frame)
            self.conversation.commit()
            self.conversation.create_response(
                instructions=instructions,
                output_modalities=[self.modalities.TEXT],
            )
            self.trace("perception.response.requested", {
                "lane": "perception", "imageBase64Bytes": len(frame), "microphoneShared": False,
                "model": self.model, "instructions": instructions,
                "frameId": context.get("frame_id") if context else None,
                "contextId": context.get("context_id") if context else None,
                "schemaVersion": context.get("schema_version") if context else None,
                "taskId": context.get("task_id") if context else None,
                "checkpointId": context.get("checkpoint_id") if context else None,
                "watchlistId": context.get("watchlist_id") if context else None,
            })
            started = self._request_started
            def install_deadline():
                self.loop.call_later(self.settings["requestTimeoutSeconds"], self._expire_request, started)
            self.loop.call_soon_threadsafe(install_deadline)
            return True
        except Exception as exc:
            self.active = False
            self.connected = False
            self.pending_frame = (frame, context)
            self.trace("perception.response.error", {"error": str(exc)})
            self._schedule_reconnect("dispatch-failed")
            return False

    def _expire_request(self, started):
        if self.closed or not self.active or started != self._request_started:
            return
        self.trace("perception.response.timeout", {"frameId": (self.active_context or {}).get("frame_id"), "timeoutSeconds": self.settings["requestTimeoutSeconds"]})
        self._generation += 1
        self.connected = False
        self._finish("")
        if self.pending_frame:
            self._schedule_reconnect("perception-timeout")

    def _finish(self, raw_text: str) -> None:
        if not self.active:
            return
        self.active = False
        self._buffer = ""
        context = self.active_context
        self.active_context = None
        self.trace("perception.raw_output", {"frameId": (context or {}).get("frame_id"), "rawOutput": raw_text})
        if context:
            decision = (
                OmniBrowserBridge.parse_ambient_output(raw_text, open_observation=context.get("observation_mode") == "open")
                if context.get("schema_version") == AMBIENT_SCHEMA_VERSION
                else OmniBrowserBridge.parse_perception_output(raw_text)
            )
            self.loop.call_soon_threadsafe(self.on_decision, decision, context)
        else:
            decision = OmniBrowserBridge.parse_proactive_gate(raw_text)
            self.loop.call_soon_threadsafe(self.on_decision, decision)
        if self.pending_frame:
            self.loop.call_soon_threadsafe(self._dispatch_pending)

    def _schedule_reconnect(self, reason: str) -> None:
        if self.closed or getattr(self, "_suspended", False) or self._blocked_failure is not None or self._reconnect_scheduled or not self.loop.is_running():
            return
        if not self.active and self.pending_frame is None:
            return
        self._reconnect_scheduled = True
        asyncio.run_coroutine_threadsafe(self._recover(reason), self.loop)

    async def _recover(self, reason: str) -> None:
        try:
            if self.closed or self._suspended or self.pending_frame is None:
                return
            self.trace("provider.session.reconnecting", {"lane": "perception", "reason": reason})
            old = self.conversation
            self.connected = False
            self.active = False
            self.active_context = None
            self._generation += 1
            self._response_starts.clear()
            if old:
                try:
                    await asyncio.to_thread(old.close)
                except Exception:
                    pass
            try:
                await self.connect()
            except Exception as exc:
                self.trace("provider.session.reconnect_failed", {"lane": "perception", "error": str(exc)})
        finally:
            self._reconnect_scheduled = False

    async def suspend(self) -> None:
        """Fence callbacks and queued frames immediately; allow later authorized demand."""
        self._suspended = True
        self._generation += 1
        self.active = self.connected = False
        self.pending_frame = self.active_context = None
        self._buffer = ""
        self._request_started = None
        self._response_starts.clear()
        conversation, self.conversation = self.conversation, None
        if conversation:
            try:
                await asyncio.wait_for(asyncio.to_thread(conversation.close), timeout=3)
            except (Exception, asyncio.TimeoutError):
                # Authorization and callback fences already took effect.
                self.trace("perception.close.pending", {"reason": "observation-stopped"})

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connectionPolicy": "on-demand",
            "connectionState": "blocked" if self._blocked_failure else "ready" if self.connected else "connecting" if self._reconnect_scheduled else "idle",
            "providerError": self._blocked_failure.public() if self._blocked_failure else None,
            "active": self.active,
            "pendingFrame": bool(self.pending_frame),
            "activeContextId": self.active_context.get("context_id") if self.active_context else None,
            "activeFrameId": self.active_context.get("frame_id") if self.active_context else None,
            "schemaVersion": (self.active_context or {}).get("schema_version"),
            "elapsedMs": round((time.monotonic() - self._request_started) * 1000) if self.active and self._request_started else None,
            "requestTimeoutMs": self.settings["requestTimeoutSeconds"] * 1000,
            "microphoneShared": False,
            "turnDetection": "disabled-explicit-response",
        }

    async def close(self) -> None:
        self.closed = True
        self.connected = False
        self._generation += 1
        if self.conversation:
            try:
                await asyncio.to_thread(self.conversation.close)
            except Exception:
                pass  # ws=None: DashScope connection was never established or already closed


class OmniBrowserBridge:
    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        instructions: str,
        emit: Callable[[dict[str, Any]], Any],
        on_transcript: Callable[[str], Any],
    ) -> None:
        import dashscope
        self.backend = os.getenv("EGOASSIST_REALTIME_BACKEND", "omni").strip().lower()
        if self.backend == "modular":
            from .modular import (
                Modalities as MultiModality, Callback as OmniRealtimeCallback,
                ModularConversation as OmniRealtimeConversation, settings as modular_settings,
            )
            self.modular_config = modular_settings()
            model = self.modular_config["textModel"]
        elif self.backend == "omni":
            from dashscope.audio.qwen_omni import (
                MultiModality, OmniRealtimeCallback, OmniRealtimeConversation,
            )
        else:
            raise ValueError("EGOASSIST_REALTIME_BACKEND must be omni or modular")

        dashscope.api_key = api_key
        self.loop = asyncio.get_running_loop()
        self.emit = emit
        self.on_transcript = on_transcript
        self.api_key = api_key
        self.model = model
        self.connecting = True
        self.connected = False
        self.closed = False
        self._dialogue_generation = 0
        self._dialogue_exchanges = 0  # completed response.done events since last reconnect
        self._blocked_failure = None
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_scheduled = False
        self._buffered_audio: deque[str] = deque()
        self._buffered_audio_limit = max(4, int(os.getenv("EGOASSIST_AUDIO_RECONNECT_CHUNKS", "40")))
        self._dropped_audio_chunks = 0
        self._Conversation = OmniRealtimeConversation
        self._CallbackBase = OmniRealtimeCallback
        self._response_lock = threading.Lock()
        self._response_pending = False
        self._active_response_ids: set[str] = set()
        self._cancelled_response_ids: set[str] = set()
        self._speech_started_response_ids: set[str] = set()
        self._active_response_requests: dict[str, _ResponseRequest] = {}
        self._queued_requests: deque[_ResponseRequest] = deque()
        self._inflight_response: _ResponseRequest | None = None
        self._response_sequence = 0
        self._cancel_when_created = False
        self._barge_in_pending = False
        self._early_terminal_response_ids: set[str] = set()
        self._response_create_timeout = _bounded_timeout(
            "EGOASSIST_RESPONSE_CREATE_TIMEOUT", 5.0, 1.0, 30.0,
        )
        self._response_finish_timeout = _bounded_timeout(
            "EGOASSIST_RESPONSE_FINISH_TIMEOUT", 45.0, 5.0, 300.0,
        )
        self._output_channels_seen: set[tuple[str, str]] = set()
        self._suppressed_output_channels_seen: set[tuple[str, str]] = set()
        self._response_text: dict[str, str] = {}
        self._context_harness: ContextHarness | None = None
        self._provider_tools: list[dict[str, Any]] = []
        self._tool_handler: Callable[[str, dict[str, Any], bool], dict[str, Any]] | None = None
        self._tool_result_handler: Callable[[str, str, Any, bool, dict[str, Any] | None], Any] | None = None
        self._result_delivery_handler: Callable[[str, str], Any] | None = None
        self._response_lifecycle_handler: Callable[[dict[str, Any], str], Any] | None = None
        self._pending_tool_calls: dict[str, dict[str, Any]] = {}
        self._tool_call_signatures: dict[str, tuple[str, str]] = {}
        self._assistant_transcript_buffers: dict[str, str] = {}
        self._last_assistant_utterance = ""
        self._last_assistant_utterance_at = 0.0
        self._replayed_compaction_id: str | None = None
        self._trace_sink: Callable[..., Any] | None = None
        self._media_lock = threading.RLock()
        self._pending_video: str | None = None
        self._input_speech_active = False
        self._input_segment_sequence = 0
        self._active_input_segment_id: str | None = None
        self._pending_input_segments: deque[str] = deque()
        self._text_conversation = None
        self._response_conversation = None
        bridge = self

        class Callback(OmniRealtimeCallback):
            def __init__(self, generation: int):
                super().__init__()
                self.generation = generation

            def on_open(self):
                return None

            def on_close(self, close_status_code=None, close_msg=None):
                if self.generation == bridge._dialogue_generation and not bridge.closed:
                    bridge._drop_open_input_segments("provider-session-closed")
                    bridge.connected = False
                    bridge._trace("provider.session.closed", {
                        "lane": "dialogue", "message": str(close_msg or ""),
                    })
                    failure = access_failure(close_msg)
                    if failure is not None:
                        bridge._block_provider(failure)
                    bridge._schedule_reconnect("provider-closed")

            def on_event(self, response):
                if self.generation != bridge._dialogue_generation or bridge.closed:
                    return
                event_type = response.get("type", "")
                if event_type == "pipeline.metrics":
                    bridge._trace("provider.pipeline.metrics", {k: v for k, v in response.items() if k != "type"})
                elif event_type == "pipeline.warning":
                    bridge._trace("provider.pipeline.warning", response)
                    bridge._schedule({"type": "audio_unavailable", "responseId": response.get("responseId"),
                                      "message": "语音暂不可用，回答已显示为文字。"})
                elif event_type == "pipeline.error":
                    bridge._trace("provider.pipeline.error", response)
                    if getattr(bridge, "backend", "omni") == "omni" and response.get("responseId"):
                        bridge._schedule({"type": "error", "message": "回答生成失败，请重试。",
                                          "responseId": response.get("responseId")})
                    else:
                        bridge._schedule({"type": "provider_unavailable", "lane": response.get("stage"),
                                          "reason": response.get("reason")})
                if event_type == "response.created":
                    bridge._response_created(response)
                elif event_type in {"response.done", "response.cancelled", "response.failed"}:
                    bridge._response_finished(response)
                if event_type == "response.audio.delta":
                    bridge.route_provider_output(event_type, response)
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = str(response.get("transcript", "")).strip()
                    if not transcript:
                        bridge._input_segment_transcribed(None, response.get("item_id"))
                        bridge._trace("audio.transcript.ignored", {"reason": "empty-provider-transcript"})
                        return
                    segment = bridge._input_segment_transcribed(transcript, response.get("item_id"))
                    barge_action = bridge.handle_user_speech_started(trigger="transcript-fallback")
                    if barge_action == "cancelled":
                        bridge._schedule({"type": "interrupted"})
                    elif barge_action in {"deferred", "protected"}:
                        bridge._schedule({"type": "barge_in_policy", "action": barge_action})
                    attribution = bridge.attribute_input_transcript(transcript)
                    bridge._trace("audio.transcript.attributed", attribution)
                    transcript_event = {
                        "type": "user_transcript",
                        "speaker": "unknown",
                        "channel": "input_audio_transcription",
                        "echoRisk": attribution["echoRisk"],
                        "text": transcript,
                    }
                    evidence_handler = getattr(bridge, "on_transcript_evidence", None)
                    if evidence_handler:
                        def accept_transcript():
                            captured = evidence_handler(transcript, segment)
                            if isinstance(captured, dict) and captured.get("turnId"):
                                transcript_event.update(turnId=captured["turnId"],
                                    timestamp=captured.get("timestamp"), persisted=True)
                            bridge._schedule(transcript_event)
                        # Publish the canonical journal identity after capture, before
                        # waiting on routing or answers. Never deduplicate by wording.
                        bridge._schedule_callback(accept_transcript)
                    else:
                        bridge._schedule(transcript_event)
                        bridge._schedule_callback(bridge.on_transcript, transcript)
                elif event_type in {"response.audio_transcript.delta", "response.audio_transcript.done"}:
                    bridge.route_provider_output(event_type, response)
                elif event_type == "response.text.delta":
                    bridge.note_output("text", response)
                    bridge._response_text_delta(response)
                elif event_type == "response.text.done":
                    bridge._response_text_done(response)
                elif event_type == "response.function_call_arguments.done":
                    bridge._handle_tool_call_event(response)
                elif event_type == "response.output_item.done":
                    bridge._handle_output_item_event(response)
                elif event_type == "input_audio_buffer.speech_started":
                    bridge._input_segment_started(response)
                    barge_action = bridge.handle_user_speech_started(trigger="provider-speech-started")
                    if barge_action == "cancelled":
                        bridge._schedule({"type": "interrupted"})
                    elif barge_action in {"deferred", "protected"}:
                        bridge._schedule({"type": "barge_in_policy", "action": barge_action})
                elif event_type == "input_audio_buffer.speech_stopped":
                    bridge._input_segment_stopped(response)
                elif event_type == "error":
                    failure = access_failure(response)
                    if failure is not None:
                        bridge._block_provider(failure)
                    elif _is_response_idle_timeout(response):
                        LOGGER.info("DashScope dialogue session reached response idle timeout; reconnecting")
                        bridge.connected = False
                        bridge._trace("provider.session.idle_timeout", {"lane": "dialogue"})
                        bridge._schedule_reconnect("response-idle-timeout")
                    elif _is_active_response_conflict(response):
                        LOGGER.info("DashScope response backpressure; requeueing inflight request")
                        bridge._response_conflict()
                    elif _is_no_active_response(response):
                        LOGGER.info("DashScope cancel/done race; reconciling stale response state")
                        bridge._response_absent_after_cancel()
                    elif _is_media_order_error(response):
                        LOGGER.error("DashScope media ordering error: %s", response)
                        bridge._trace("provider.media.ordering_error", {
                            "message": _provider_error_message(response),
                            "recovery": "dialogue-reconnect",
                        })
                        bridge._drop_open_input_segments("provider-media-ordering-error")
                        bridge.connected = False
                        bridge._schedule({"type": "error", "message": _provider_error_message(response)})
                        bridge._schedule_reconnect("media-ordering-error")
                    else:
                        LOGGER.error("DashScope realtime error: %s", response)
                        message = _provider_error_message(response)
                        error = response.get("error") or {}
                        code = error.get("code") if isinstance(error, dict) else None
                        event_id = response.get("event_id")
                        serving_error = "InternalError.Algo.ModelServingError" in message
                        bridge._trace("provider.error", {
                            "message": message, "payloadType": event_type,
                            "code": code, "eventId": event_id, "retryable": serving_error,
                        })
                        bridge._schedule({
                            "type": "error",
                            "message": "云端实时模型处理失败，本次回答可能中断，请重试。"
                            if serving_error else message,
                            "code": code, "eventId": event_id, "retryable": serving_error,
                        })

        self._Callback = Callback
        self.conversation = None
        self.modalities = MultiModality
        self.voice = voice
        self.instructions = instructions
        self.last_proactive_check = 0.0
        self._latest_perception_frame: dict[str, Any] | None = None
        self._perception_context_provider: Callable[[str, datetime], dict[str, Any] | None] | None = None
        self._perception_result_handler: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
        self.perception = _OmniPerceptionLane(
            api_key=api_key,
            model=model,
            voice=voice,
            loop=self.loop,
            conversation_cls=OmniRealtimeConversation,
            callback_cls=OmniRealtimeCallback,
            modalities=MultiModality,
            on_decision=self._handle_proactive_decision,
            trace=self._trace,
        )
        self.ambient_perception = _OmniPerceptionLane(
            api_key=api_key, model=model, voice=voice, loop=self.loop,
            conversation_cls=OmniRealtimeConversation, callback_cls=OmniRealtimeCallback,
            modalities=MultiModality, on_decision=self._handle_proactive_decision,
            trace=lambda event, payload, **kw: self._trace(event, {**payload, "lane": "ambient"}, **kw),
        )
        if self.backend == "modular":
            self.perception.model = self.modular_config["visionModel"]
            self.ambient_perception.model = self.modular_config["visionModel"]
        self._ambient_context_provider = None
        self.last_ambient_check = 0.0

    async def connect(self) -> None:
        try:
            await self._connect_dialogue()
        except (ConnectionError, OSError, TimeoutError, requests.RequestException) as exc:
            self.connecting = False
            message = str(exc)
            self._trace("provider.session.unavailable", {
                "lane": "dialogue", "category": "network", "error": message,
            })
            await self.emit({"type": "provider_unavailable", "lane": "dialogue", "reason": message})
            await self.emit({"type": "ready", "lanes": self.provider_lanes_snapshot()})
            return
        # The isolated observation lane connects when its first frame is requested.
        self.connecting = False
        if self.closed:
            return
        await self.emit({"type": "ready", "lanes": self.provider_lanes_snapshot()})
        for call_id, pending in list(getattr(self, "_pending_tool_calls", {}).items()):
            if getattr(self, "auto_approve_tools", False):
                await asyncio.to_thread(self.confirm_tool, call_id, True)
                continue
            await self.emit({
                "type": "tool_confirmation_required",
                "callId": call_id,
                "tool": pending["name"],
                "arguments": pending["arguments"],
                "recovered": pending.get("generation") is None,
                "origin": pending.get("origin", {}),
            })

    def _new_dialogue_conversation(self, generation: int):
        conversation = self._Conversation(
            model=self.model,
            callback=self._Callback(generation),
            api_key=self.api_key,
            url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
        )
        return _observe_access_errors(conversation, lambda failure: self._block_provider(failure)
                                      if generation == self._dialogue_generation and not self.closed else None)

    def _block_provider(self, failure):
        if getattr(self, "_blocked_failure", None) is not None:
            return
        self._blocked_failure = failure
        self.connected = False
        self._buffered_audio.clear()
        self._trace("provider.account.blocked", {"lane": "dialogue", **failure.public()})
        self._schedule({"type": "provider_unavailable", "lane": "dialogue",
                        "reason": str(failure), "retryable": False, "code": failure.code})

    def _configure_dialogue(self, conversation) -> None:
        from egoassist.core.settings import integer
        silence_ms = integer('EGOASSIST_SPEECH_END_SILENCE_MS', 1200, minimum=300, maximum=3000)
        tools = [] if getattr(self, "tool_model", None) else getattr(self, "_provider_tools", [])
        signature = (conversation, self.voice, self.instructions, json.dumps(tools, sort_keys=True), silence_ms)
        if getattr(self, "_last_dialogue_configuration", None) == signature:
            return
        conversation.update_session(
            output_modalities=[self.modalities.AUDIO, self.modalities.TEXT],
            voice=self.voice,
            instructions=self.instructions,
            enable_input_audio_transcription=True,
            enable_turn_detection=True,
            turn_detection_type="server_vad",
            turn_detection_threshold=0.2,
            turn_detection_silence_duration_ms=silence_ms,
            turn_detection_param={"create_response": False, "interrupt_response": not getattr(self, "interaction_managed", False)},
            tools=tools,
        )
        self._last_dialogue_configuration = signature
        self._trace("model.prompt", {"channel": "session.update", "instructions": self.instructions,
                                    "tools": tools, "model": getattr(self, "model", None),
                                    "speechEndSilenceMs": silence_ms})

    def _replay_context(self, conversation) -> int:
        harness = getattr(self, "_context_harness", None)
        if harness is None:
            return 0
        items = harness.provider_replay_items()
        for item in items:
            if self.closed or conversation is not self.conversation:
                return 0
            conversation.create_item(item)
        self._replayed_compaction_id = harness.projection_checkpoint_id
        self._dialogue_exchanges = 0  # reset after each fresh DashScope session
        self._trace("context.replayed", {
            "sessionId": harness.session_id,
            "itemCount": len(items),
            "generation": self._dialogue_generation,
            "compactionId": self._replayed_compaction_id,
        })
        return len(items)

    async def _connect_dialogue(self) -> None:
        async with self._reconnect_lock:
            if getattr(self, "_blocked_failure", None) is not None:
                raise self._blocked_failure
            if self.closed or self.connected:
                return
            self._dialogue_generation += 1
            generation = self._dialogue_generation
            conversation = self._new_dialogue_conversation(generation)
            self.conversation = conversation
            started = time.monotonic()
            await self.emit({"type": "connection_progress", "stage": "provider-connect"})
            await asyncio.to_thread(_connect_with_resolved_ip, conversation)
            if self._blocked_failure is not None:
                await asyncio.to_thread(conversation.close)
                raise self._blocked_failure
            if generation != self._dialogue_generation or self.closed:
                await asyncio.to_thread(conversation.close)
                return
            await self.emit({"type": "connection_progress", "stage": "context-restore"})
            # The SDK sends synchronously; history replay must not block HTTP,
            # WebSocket heartbeats, or cancellation on the application loop.
            await asyncio.to_thread(self._configure_dialogue, conversation)
            await asyncio.to_thread(self._replay_context, conversation)
            if generation != self._dialogue_generation or self.closed:
                await asyncio.to_thread(conversation.close)
                return
            self.connected = True
            self._reset_scheduler_after_reconnect()
            self._replayed_audio_samples = 0
            while self._buffered_audio:
                audio = self._buffered_audio.popleft()
                conversation.append_audio(audio)
                self._replayed_audio_samples += len(base64.b64decode(audio)) // 2
            self._trace("provider.session.ready", {
                "lane": "dialogue", "generation": generation,
                "startupMs": round((time.monotonic() - started) * 1000, 1),
                "backend": getattr(self, "backend", "omni"),
                "turnDetection": "webrtc-vad-segment-asr" if getattr(self, "backend", "omni") == "modular" else "server-vad-explicit-response",
            })

    def _schedule_reconnect(self, reason: str) -> None:
        if self.closed or getattr(self, "_blocked_failure", None) is not None or self._reconnect_scheduled or not self.loop.is_running():
            return
        self._reconnect_scheduled = True
        asyncio.run_coroutine_threadsafe(self._recover_dialogue(reason), self.loop)

    async def _recover_dialogue(self, reason: str) -> None:
        try:
            if self.closed:
                return
            await self.emit({"type": "reconnecting", "lane": "dialogue", "reason": reason})
            self._trace("provider.session.reconnecting", {"lane": "dialogue", "reason": reason})
            self._drop_open_input_segments(f"provider-reconnect:{reason}")
            self._persist_active_interruptions(f"provider-reconnect:{reason}")
            text_conversation = getattr(self, "_text_conversation", None)
            if text_conversation is not None:
                await text_conversation.aclose()
                self._text_conversation = None
            self._response_conversation = None
            old = self.conversation
            self.connected = False
            self._dialogue_generation += 1
            if old:
                try:
                    await asyncio.to_thread(old.close)
                except Exception:
                    pass
            try:
                await self._connect_dialogue()
            except Exception as exc:
                self._trace("provider.session.reconnect_failed", {"lane": "dialogue", "error": str(exc)})
                await self.emit({"type": "error", "message": f"实时对话通道重连失败：{exc}"})
                return
            await self.emit({"type": "ready", "reconnected": True, "lanes": self.provider_lanes_snapshot()})
        finally:
            self._reconnect_scheduled = False

    def _reset_scheduler_after_reconnect(self) -> None:
        with self._response_lock:
            self._response_pending = False
            self._active_response_ids.clear()
            self._cancelled_response_ids.clear()
            self._speech_started_response_ids.clear()
            self._active_response_requests.clear()
            self._early_terminal_response_ids.clear()
            self._inflight_response = None
            self._cancel_when_created = False
            self._barge_in_pending = False
        self._schedule_queued_dispatch()

    def _input_segment_started(self, payload: dict[str, Any]) -> str:
        with self._media_lock:
            self._input_segment_sequence = getattr(self, "_input_segment_sequence", 0) + 1
            segment_id = f"segment-{self._dialogue_generation}-{self._input_segment_sequence}"
            self._active_input_segment_id = segment_id
            self._input_speech_active = True
            pending = getattr(self, "_pending_input_segments", None)
            if pending is None:
                pending = deque()
                self._pending_input_segments = pending
            pending.append(segment_id)
            contexts = getattr(self, "_input_segment_contexts", None)
            if contexts is None:
                contexts = self._input_segment_contexts = {}
            contexts[segment_id] = {"segmentId": segment_id, "audioStartMs": payload.get("audio_start_ms"),
                                    "providerItemId": payload.get("item_id"), "generation": self._dialogue_generation}
        self._trace("input.segment.started", {
            "segmentId": segment_id,
            "providerItemId": payload.get("item_id"),
            "audioStartMs": payload.get("audio_start_ms"),
        })
        self._flush_pending_video_for_active_segment()
        return segment_id

    def _flush_pending_video_for_active_segment(self) -> bool:
        with self._media_lock:
            if not getattr(self, "_pending_video", None) or not self._input_speech_active:
                return False
            frame = self._pending_video
            try:
                self.conversation.append_video(frame)
                self._pending_video = None
            except Exception as exc:
                self.connected = False
                self._trace("provider.media.append_failed", {
                    "media": "video", "error": str(exc)[:240], "recovery": "dialogue-reconnect",
                })
                self._schedule_reconnect("media-append-failed")
                return False
        return True

    def _input_segment_stopped(self, payload: dict[str, Any]) -> None:
        with self._media_lock:
            contexts = getattr(self, "_input_segment_contexts", {})
            item_id = payload.get("item_id")
            active = getattr(self, "_active_input_segment_id", None)
            segment_id = next((sid for sid, row in contexts.items()
                               if item_id and row.get("providerItemId") == item_id), None)
            if segment_id is None:
                # Some provider stops replace the provisional VAD-start item ID.
                # Rebind only the sole open interval; never guess between intervals.
                opened = [sid for sid, row in contexts.items() if row.get("audioEndMs") is None]
                if opened == [active]:
                    segment_id = active
            if segment_id in contexts:
                row = contexts[segment_id]
                row["audioEndMs"] = payload.get("audio_end_ms")
                if item_id and row.get("providerItemId") != item_id:
                    row["startProviderItemId"] = row.get("providerItemId")
                    row["providerItemId"] = item_id
            if segment_id == active:
                self._active_input_segment_id = None
                self._input_speech_active = False
        self._trace("input.segment.stopped", {
            "segmentId": segment_id,
            "providerItemId": payload.get("item_id"),
            "audioEndMs": payload.get("audio_end_ms"),
        })

    def _input_segment_transcribed(self, transcript: str | None, item_id: str | None = None) -> dict:
        with self._media_lock:
            pending = getattr(self, "_pending_input_segments", deque())
            contexts = getattr(self, "_input_segment_contexts", {})
            segment_id = next((sid for sid in pending if contexts.get(sid, {}).get("providerItemId") == item_id), None) if item_id else (pending[0] if len(pending) == 1 else None)
            if segment_id:
                pending.remove(segment_id)
            if segment_id and segment_id == self._active_input_segment_id:
                self._active_input_segment_id = None
                self._input_speech_active = False
        self._trace("input.segment.transcribed" if transcript else "input.segment.dropped", {
            "segmentId": segment_id,
            "characters": len(transcript or ""),
            "reason": None if transcript else "empty-provider-transcript",
        })
        return getattr(self, "_input_segment_contexts", {}).pop(segment_id, {})

    def _drop_open_input_segments(self, reason: str) -> None:
        media_lock = getattr(self, "_media_lock", None)
        if media_lock is None:
            media_lock = threading.Lock()
            self._media_lock = media_lock
        with media_lock:
            pending = getattr(self, "_pending_input_segments", deque())
            segment_ids = list(pending)
            pending.clear()
            getattr(self, "_input_segment_contexts", {}).clear()
            self._active_input_segment_id = None
            self._input_speech_active = False
        for segment_id in segment_ids:
            self._trace("input.segment.dropped", {"segmentId": segment_id, "reason": str(reason)})

    def has_unresolved_user_input(self) -> bool:
        """Return true from VAD start until transcription or an audited drop."""

        with self._media_lock:
            return bool(
                getattr(self, "_input_speech_active", False)
                or getattr(self, "_pending_input_segments", ())
            )

    def append_audio(self, audio_b64: str) -> bool:
        if not audio_b64 or getattr(self, "_blocked_failure", None) is not None:
            return False
        if not getattr(self, "connected", True) or not self.conversation:
            self._buffer_audio_for_reconnect(audio_b64)
            self._schedule_reconnect("audio-arrived-while-disconnected")
            return False
        video_flushed = False
        try:
            with self._media_lock:
                self.conversation.append_audio(audio_b64)
                if self._pending_video and getattr(self, "_input_speech_active", True):
                    self.conversation.append_video(self._pending_video)
                    self._pending_video = None
                    video_flushed = True
        except Exception:
            self._buffer_audio_for_reconnect(audio_b64)
            self.connected = False
            self._schedule_reconnect("media-append-failed")
        return video_flushed

    def _buffer_audio_for_reconnect(self, audio_b64: str) -> None:
        limit = int(getattr(self, "_buffered_audio_limit", 40))
        backlog = getattr(self, "_buffered_audio", None)
        if backlog is None:
            backlog = deque()
            self._buffered_audio = backlog
        if len(backlog) >= limit:
            dropped = backlog.popleft()
            self._dropped_audio_chunks = int(getattr(self, "_dropped_audio_chunks", 0)) + 1
            payload = {
                "media": "audio",
                "reason": "provider-reconnect-buffer-overflow",
                "droppedChunks": self._dropped_audio_chunks,
                "droppedBase64Characters": len(dropped),
                "bufferLimitChunks": limit,
            }
            self._trace("provider.media.gap", payload)
            self._schedule({"type": "media_gap", **payload})
        backlog.append(audio_b64)

    def append_video(self, image_b64: str) -> bool:
        if not image_b64:
            return False
        with self._media_lock:
            # The official camera sample sends a frame only from the microphone
            # loop, immediately after a fresh audio chunk. A prior audio append
            # may already have been auto-committed by server VAD.
            self._pending_video = image_b64
            self._latest_perception_frame = {
                "image": image_b64,
                "frameId": f"frame-{uuid4().hex}",
                "capturedAt": datetime.now(timezone.utc),
            }
            return False

    def maybe_check_proactively(self, interval_seconds: float = 4.0) -> None:
        now = time.monotonic()
        with self._response_lock:
            if now - self.last_proactive_check < interval_seconds:
                return
            self.last_proactive_check = now
        frame = getattr(self, "_latest_perception_frame", None)
        if not frame:
            return
        if isinstance(frame, str):
            self.perception.request(frame)
            return
        context = None
        if self._perception_context_provider is not None:
            try:
                context = self._perception_context_provider(frame["frameId"], frame["capturedAt"])
            except Exception as exc:
                self._trace("perception.frame.suppressed", {
                    "frameId": frame.get("frameId"), "reason": "context-provider-failed",
                    "error": str(exc)[:240],
                })
                return
            if context is None:
                # Covers both "no task is active" and "ambient declined this
                # frame" (already submitted, or ambient off), so the reason does
                # not claim more than it knows.
                self._trace("perception.frame.suppressed", {
                    "frameId": frame.get("frameId"), "reason": "no-active-observation-context",
                })
                return
        self.perception.request(frame["image"], context)

    def maybe_observe_ambient(self, interval_seconds: float) -> None:
        """Ambient cadence is independent of task checks and dialogue output."""
        frame = self._latest_perception_frame
        if not frame or not self._ambient_context_provider or self.closed:
            return
        now = time.monotonic()
        if now - self.last_ambient_check < interval_seconds:
            return
        context = self._ambient_context_provider(frame["frameId"], frame["capturedAt"])
        if context is None:
            return
        self.last_ambient_check = now
        self.ambient_perception.request(frame["image"], context)

    def request_response(
        self,
        instructions: str,
        priority: int = 50,
        interrupt: bool = False,
        speech_policy: str = "immediate",
        reason: str = "unspecified",
        max_protection_ms: int = 2500,
        output_mode: str = "audio",
        purpose: str = "conversation",
        origin: dict[str, Any] | None = None,
    ) -> bool:
        """Request one response, coalescing work while the provider is busy."""
        if purpose == "proactive-gate":
            raise ValueError("proactive-gate must use the isolated perception lane")
        if speech_policy not in {"immediate", "finish-clause", "protected"}:
            raise ValueError(f"unknown speech policy: {speech_policy}")
        if output_mode not in {"audio", "text"}:
            raise ValueError(f"unknown output mode: {output_mode}")
        should_dispatch = False
        should_cancel = False
        with self._response_lock:
            self._response_sequence += 1
            request = _ResponseRequest(
                instructions, priority, self._response_sequence, speech_policy, reason,
                time.monotonic(), max(250, min(5000, int(max_protection_ms))), output_mode, purpose,
                dict(origin) if origin else None,
            )
            self._trace("response.requested", {"sequence": request.sequence, "priority": priority, "speechPolicy": speech_policy, "reason": reason, "outputMode": output_mode, "purpose": purpose})
            disconnected = not getattr(self, "connected", True)
            busy = disconnected or self._response_pending or bool(self._active_response_ids)
            if busy:
                queue = self._queued_requests
                if interrupt:
                    # interrupt=True → caller intends a correction/redirect: replace
                    # the entire queue so the new request runs next.
                    displaced = list(queue)
                    queue.clear()
                    queue.append(request)
                    self._trace("response.queued", {"sequence": request.sequence, "mode": "replace", "displaced": len(displaced)})
                    for old in displaced:
                        self._return_result_to_inbox(old, "queued-response-replaced")
                elif len(queue) < integer("EGOASSIST_RESPONSE_QUEUE_LIMIT", 16):
                    # Additional replies wait in a configurable bounded queue.
                    queue.append(request)
                    self._trace("response.queued", {"sequence": request.sequence, "mode": "append", "depth": len(queue)})
                else:
                    # Queue full: drop the new request; preserve the existing chain.
                    self._return_result_to_inbox(request, "queued-response-dropped-full")
                    self._trace("response.queue.full", {"sequence": request.sequence})
                if interrupt:
                    if self._active_response_ids:
                        should_cancel = True
                    elif self._response_pending:
                        self._cancel_when_created = True
            else:
                self._response_pending = True
                self._inflight_response = request
                should_dispatch = True
        if should_cancel:
            try:
                self._cancel_provider_response()
                self._trace("response.cancel.requested", {"reason": "higher-priority-request"})
            except Exception:
                pass
        if should_dispatch:
            self._send_response(request)
        elif disconnected:
            self._schedule_reconnect("response-requested-while-disconnected")
        elif interrupt and getattr(self, "_tool_runner", None):
            self._tool_runner.cancel_selection()
        return should_dispatch

    def handle_user_speech_started(self, *, trigger: str = "provider-speech-started") -> str:
        """Apply an explicit barge-in policy while continuing microphone capture."""
        started = time.perf_counter()
        with self._response_lock:
            response_ids = list(self._active_response_ids)
        # Provider completion can precede playback completion. Always stop the
        # device queue, even when no cloud generation remains to cancel.
        self._schedule({"type": "playback_stop", "trigger": trigger, "responseIds": response_ids})
        self._trace("playback.stop.requested", {"trigger": trigger,
                    "dispatchMs": round((time.perf_counter() - started) * 1000, 3)})
        if getattr(self, "interaction_managed", False):
            return "paused"
        with self._response_lock:
            response_keys = set(self._active_response_ids)
            if self._response_pending and not response_keys:
                response_keys.add("provider-pending")
            handled = getattr(self, "_speech_started_response_ids", None)
            if handled is None:
                handled = set()
                self._speech_started_response_ids = handled
            if response_keys and response_keys.issubset(handled):
                return "already-handled"
            handled.update(response_keys)
            requests = list(getattr(self, "_active_response_requests", {}).values())
            current = max(requests, key=lambda item: item.sequence) if requests else self._inflight_response
            policy = current.speech_policy if current else "immediate"
            # A pending protected response has no audible warning to preserve.
            if policy == "protected" and not requests:
                policy = "immediate"
            reason = current.reason if current else "provider-or-untracked-response"
            if policy == "protected":
                self._trace("barge_in.protected", {"policy": policy, "reason": reason, "maxProtectionMs": current.max_protection_ms, "trigger": trigger})
                self._schedule_protection_expiry(current.max_protection_ms)
                return "protected"
            if policy == "finish-clause":
                self._barge_in_pending = True
                self._trace("barge_in.deferred", {"policy": policy, "reason": reason, "trigger": trigger})
                return "deferred"
        if self.cancel_response(drop_queued_below=100):
            self._trace("barge_in.cancelled", {"policy": "immediate", "reason": reason, "trigger": trigger})
            return "cancelled"
        return "idle"

    def note_assistant_transcript(
        self,
        text: str,
        done: bool = False,
        *,
        response_id: str | None = None,
        purpose: str = "conversation",
    ) -> None:
        key = response_id or "__unattributed__"
        buffers = self._assistant_transcript_buffers
        if done:
            buffered = buffers.get(key, "")
            completed = text or buffered
            self._last_assistant_utterance = completed
            self._last_assistant_utterance_at = time.monotonic()
            if completed:
                buffers[key] = completed
        else:
            buffers[key] = buffers.get(key, "") + text
        with self._response_lock:
            should_cancel = bool(getattr(self, "_barge_in_pending", False) and (done or any(mark in text for mark in "。！？.!?；;")))
            if should_cancel:
                self._barge_in_pending = False
        if should_cancel and self.cancel_response(drop_queued_below=100):
            self._trace("barge_in.clause_boundary", {"boundaryText": text[-40:]})
            self._schedule({"type": "interrupted"})

    def attribute_input_transcript(self, text: str) -> dict[str, Any]:
        """Mark likely acoustic echo without suppressing possible user repetition."""
        def normalize(value: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())
        incoming = normalize(str(text))
        assistant = normalize(getattr(self, "_last_assistant_utterance", ""))
        utterance_at = getattr(self, "_last_assistant_utterance_at", 0.0)
        age_ms = max(0.0, (time.monotonic() - utterance_at) * 1000)
        similarity = (
            difflib.SequenceMatcher(None, incoming, assistant).ratio()
            if len(incoming) >= 4 and len(assistant) >= 4 and age_ms <= 5000
            else 0.0
        )
        return {
            "speaker": "unknown",
            "sourceChannel": "input_audio_transcription",
            "echoRisk": "suspected" if similarity >= 0.82 else "low",
            "assistantSimilarity": round(similarity, 4),
            "assistantAgeMs": round(age_ms, 2) if utterance_at else None,
            "policy": "observe-only-never-drop",
        }

    def note_output(self, channel: str, payload: dict[str, Any]) -> None:
        response_id = self._payload_response_id(payload)
        if not response_id:
            return
        key = (response_id, channel)
        seen = getattr(self, "_output_channels_seen", set())
        if key in seen:
            return
        seen.add(key)
        self._output_channels_seen = seen
        with self._response_lock:
            request = getattr(self, "_active_response_requests", {}).get(response_id)
        latency_ms = (time.monotonic() - request.requested_at) * 1000 if request and request.requested_at else None
        sink = getattr(self, "_trace_sink", None)
        if sink:
            sink(
                f"response.first_{channel}", "realtime-provider",
                {"channel": channel, "sequence": request.sequence if request else None}, [],
                response_id=response_id, latency_ms=latency_ms,
            )

    @staticmethod
    def _payload_response_id(payload: dict[str, Any]) -> str | None:
        nested = payload.get("response", {})
        nested_id = nested.get("id") if isinstance(nested, dict) else None
        response_id = payload.get("response_id") or nested_id
        return str(response_id) if response_id else None

    def _request_for_output(self, payload: dict[str, Any]) -> tuple[str | None, _ResponseRequest | None]:
        """Resolve output ownership by exact provider response ID; never guess."""
        response_id = self._payload_response_id(payload)
        if not response_id:
            return None, None
        with self._response_lock:
            return response_id, getattr(self, "_active_response_requests", {}).get(response_id)

    def route_provider_output(self, event_type: str, payload: dict[str, Any]) -> bool:
        """Route only classified speech responses to the browser-facing channels."""
        response_id, request = self._request_for_output(payload)
        channel = "audio" if event_type == "response.audio.delta" else "transcript"
        with self._response_lock:
            cancel_requested = bool(
                response_id and response_id in getattr(self, "_cancelled_response_ids", set())
            )
        if cancel_requested:
            key = (response_id, channel)
            seen = getattr(self, "_suppressed_output_channels_seen", set())
            if key not in seen:
                seen.add(key)
                self._suppressed_output_channels_seen = seen
                self._trace(
                    "response.output.suppressed",
                    {
                        "channel": channel,
                        "eventType": event_type,
                        "purpose": request.purpose if request else "unknown",
                        "outputMode": request.output_mode if request else "unknown",
                        "reason": "cancel-requested",
                    },
                    response_id=response_id,
                )
            return False
        visible = bool(request and request.output_mode == "audio" and request.purpose != "proactive-gate")
        if not visible:
            # Some realtime providers return a text-only request on the audio
            # transcript channel. Treat it as an internal gate artifact while
            # still blocking both caption and playback delivery.
            if request and request.purpose == "proactive-gate" and channel == "transcript":
                if event_type.endswith(".done"):
                    self._response_text_done(payload)
                else:
                    self._response_text_delta(payload)
            key = (response_id or "unattributed", channel)
            seen = getattr(self, "_suppressed_output_channels_seen", set())
            if key not in seen:
                seen.add(key)
                self._suppressed_output_channels_seen = seen
                self._trace(
                    "response.output.suppressed",
                    {
                        "channel": channel,
                        "eventType": event_type,
                        "purpose": request.purpose if request else "unattributed",
                        "outputMode": request.output_mode if request else "unknown",
                        "reason": "non-user-visible-response" if request else "unattributed-response-id",
                    },
                    response_id=response_id,
                )
            return False

        self.note_output(channel, payload)
        if channel == "audio":
            self._schedule({
                "type": "audio", "audio": payload.get("delta", ""),
                "purpose": request.purpose, "responseId": response_id,
                "origin": request.correlation,
            })
            return True

        transcript = str(payload.get("delta") or payload.get("transcript", ""))
        done = event_type.endswith(".done")
        self._schedule({
            "type": "assistant_transcript",
            "speaker": "assistant",
            "channel": "spoken_audio",
            "purpose": request.purpose,
            "responseId": response_id,
            "origin": request.correlation,
            "text": transcript,
            "done": done,
        })
        self.note_assistant_transcript(
            transcript,
            done,
            response_id=response_id,
            purpose=request.purpose,
        )
        return True

    def _handle_output_item_event(self, payload: dict[str, Any]) -> bool:
        item = payload.get("item", {})
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return False
        self._handle_tool_call_event({
            **item,
            "response_id": payload.get("response_id"),
        })
        return True

    def _handle_tool_call_event(self, payload: dict[str, Any]) -> None:
        call_id = str(payload.get("call_id", "")).strip()
        name = str(payload.get("name", "")).strip()
        raw_arguments = payload.get("arguments", "{}")
        response_id = self._payload_response_id(payload)
        with self._response_lock:
            source_request = self._active_response_requests.get(response_id)
            if source_request is None and self._inflight_response is not None:
                source_request = self._inflight_response
        origin = dict(source_request.origin) if source_request and source_request.origin else {}
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            arguments = None
        if not call_id or not name or not isinstance(arguments, dict):
            self._trace("tool.call.invalid", {
                "callId": call_id or None, "name": name or None, "arguments": raw_arguments,
            }, response_id=response_id)
            if call_id and name:
                claim, _ = self._claim_tool_call(call_id, (name, str(raw_arguments)))
                if claim == "new":
                    if self._context_harness:
                        self._context_harness.record_tool_call(call_id, name, {"invalidArguments": raw_arguments},
                                                              metadata={"origin": origin})
                    self._complete_tool_call(call_id, name,
                        {"status": "error", "error": "Tool arguments must be a valid JSON object. Correct the arguments before trying again."},
                        True, origin=origin)
            return
        signature = (name, json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        claim, known = self._claim_tool_call(call_id, signature)
        if claim != "new":
            if claim == "duplicate":
                self._trace("tool.call.duplicate_suppressed", {
                    "callId": call_id, "name": name,
                }, response_id=response_id)
            else:
                self._trace("tool.call.conflict_rejected", {
                    "callId": call_id,
                    "originalName": known[0],
                    "conflictingName": name,
                }, response_id=response_id)
                self._schedule({
                    "type": "error",
                    "message": "Provider reused a tool call ID with conflicting arguments; execution was blocked.",
                })
            return
        if self._context_harness:
            self._context_harness.record_tool_call(
                call_id, name, arguments, response_id=response_id, metadata={"origin": origin},
            )
        self._trace("tool.call.received", {
            "callId": call_id, "name": name, "arguments": arguments,
        }, response_id=response_id)
        self._schedule({"type": "tool_status", "callId": call_id, "tool": name,
                        "arguments": arguments, "origin": origin, "status": "received"})
        if self._tool_handler is None:
            self._complete_tool_call(
                call_id, name, {"status": "error", "error": "tool handler unavailable"},
                True, origin=origin,
            )
            return
        try:
            automatic = getattr(self, "auto_approve_tools", False)
            if automatic and self._context_harness:
                self._context_harness.journal.append("tool_execution", {
                    "callId": call_id, "accepted": True, "policy": "automatic", "origin": origin,
                }, idempotency_key=f"tool-execution:{call_id}")
            self._schedule({"type": "tool_status", "callId": call_id, "tool": name,
                            "origin": origin, "status": "running"})
            result = self._invoke_tool_handler(name, arguments, automatic, origin)
            if not isinstance(result, dict):
                raise ValueError("tool handler must return a result object")
        except Exception as exc:
            self._complete_tool_call(
                call_id, name, {"status": "error", "error": str(exc)}, True, origin=origin,
            )
            return
        if result.get("status") == "confirmation_required":
            self._pending_tool_calls[call_id] = {
                "name": name, "arguments": arguments, "generation": self._dialogue_generation,
                "origin": origin,
            }
            if self._context_harness:
                self._context_harness.journal.append("tool_confirmation", {
                    "callId": call_id, "name": name, "arguments": arguments, "status": "pending",
                    "origin": origin, "description": result.get("description", ""),
                }, idempotency_key=f"tool-confirmation:{call_id}")
            self._schedule({
                "type": "tool_confirmation_required",
                "callId": call_id,
                "tool": name,
                "arguments": arguments,
                "origin": origin,
                "description": result.get("description", ""),
            })
            return
        self._complete_tool_call(call_id, name, result, False, origin=origin)

    def _invoke_tool_handler(
        self,
        name: str,
        arguments: dict[str, Any],
        confirmed: bool,
        origin: dict[str, Any],
    ) -> dict[str, Any]:
        handler = self._tool_handler
        if handler is None:
            raise RuntimeError("tool handler unavailable")
        parameters = inspect.signature(handler).parameters.values()
        accepts_origin = any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters) or len(
            inspect.signature(handler).parameters
        ) >= 4
        return handler(name, arguments, confirmed, origin) if accepts_origin else handler(
            name, arguments, confirmed,
        )

    def _claim_tool_call(
        self,
        call_id: str,
        signature: tuple[str, str],
    ) -> tuple[str, tuple[str, str] | None]:
        durable: tuple[str, str] | None = None
        with self._response_lock:
            known = getattr(self, "_tool_call_signatures", {}).get(call_id)
        harness = getattr(self, "_context_harness", None)
        if known is None and harness:
            for entry in reversed(harness.journal.entries()):
                if entry.get("type") == "tool_call" and str(entry.get("callId", "")) == call_id:
                    durable = (
                        str(entry.get("name", "")),
                        json.dumps(entry.get("arguments", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    )
                    break
        with self._response_lock:
            values = getattr(self, "_tool_call_signatures", None)
            if values is None:
                values = {}
                self._tool_call_signatures = values
            known = values.get(call_id) or durable
            if known is not None:
                values[call_id] = known
                return ("duplicate" if known == signature else "conflict"), known
            values[call_id] = signature
            while len(values) > 512:
                values.pop(next(iter(values)))
            return "new", None

    def _remember_tool_call_signature(self, call_id: str, signature: tuple[str, str]) -> None:
        with self._response_lock:
            values = getattr(self, "_tool_call_signatures", None)
            if values is None:
                values = {}
                self._tool_call_signatures = values
            values[call_id] = signature
            while len(values) > 512:
                values.pop(next(iter(values)))

    def confirm_tool(self, call_id: str, accepted: bool) -> bool:
        if not isinstance(accepted, bool):
            raise ValueError("accepted must be a boolean")
        with self._response_lock:
            pending = self._pending_tool_calls.get(str(call_id))
            if pending is not None:
                if self._context_harness:
                    self._context_harness.journal.append("tool_execution", {
                        "callId": call_id, "accepted": accepted, "origin": pending.get("origin", {}),
                    }, idempotency_key=f"tool-execution:{call_id}")
                self._pending_tool_calls.pop(str(call_id))
        if pending is None:
            return bool(self._context_harness and any(
                entry.get("callId") == call_id and entry.get("type") in {"tool_result", "tool_execution"}
                for entry in self._context_harness.journal.entries()))
        name = pending["name"]
        arguments = pending["arguments"]
        origin = dict(pending.get("origin") or {})
        provider_call_active = pending.get("generation") == self._dialogue_generation
        self._schedule({"type": "tool_status", "callId": call_id, "tool": name,
                        "arguments": arguments, "origin": origin, "status": "running"})
        if not accepted:
            self._complete_tool_call(
                call_id, name, {"status": "rejected"}, False, provider_call_active, origin,
            )
            return True
        if self._tool_handler is None:
            self._complete_tool_call(
                call_id, name, {"status": "error", "error": "tool handler unavailable"},
                True, provider_call_active, origin,
            )
            return True
        try:
            result = self._invoke_tool_handler(name, arguments, True, origin)
            self._complete_tool_call(call_id, name, result, False, provider_call_active, origin)
        except Exception as exc:
            self._complete_tool_call(
                call_id, name, {"status": "error", "error": str(exc)},
                True, provider_call_active, origin,
            )
        return True

    def _complete_tool_call(
        self,
        call_id: str,
        name: str,
        result: Any,
        is_error: bool,
        provider_call_active: bool = True,
        origin: dict[str, Any] | None = None,
        continue_response: bool = True,
        publish_context: bool = True,
    ) -> dict[str, Any]:
        journal_entry = None
        if self._context_harness:
            journal_entry = self._context_harness.record_tool_result(
                call_id, name, result, is_error=is_error, metadata={"origin": dict(origin or {})},
            )
        tool_result_handler = getattr(self, "_tool_result_handler", None)
        result_handler_value = None
        if tool_result_handler:
            try:
                result_handler_value = tool_result_handler(call_id, name, result, is_error, journal_entry)
            except Exception as exc:
                self._trace("timeline.memory.tool-effect.failed", {
                    "callId": call_id, "name": name, "error": str(exc)[:240],
                })
        if isinstance(result_handler_value, dict) and result_handler_value.get("resultId"):
            origin = {**dict(origin or {}), "resultId": str(result_handler_value["resultId"])}
        self._trace("tool.result.committed", {
            "callId": call_id, "name": name, "result": result, "isError": is_error,
        })
        self._schedule({"type": "tool_status", "callId": call_id, "tool": name,
                        "origin": origin or {}, "status": "failed" if is_error else
                        result.get("status", "completed") if isinstance(result, dict) else "completed",
                        "result": result, "isError": is_error})
        if publish_context and self.conversation and self.connected and provider_call_active:
            self.conversation.create_item({
                "id": "item_" + uuid4().hex,
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
        elif publish_context and self.conversation and self.connected:
            self.inject_context_data("recovered_tool_result", {
                "callId": call_id, "name": name, "result": result, "isError": is_error,
            })
        if self.conversation and self.connected and continue_response:
            succeeded = tool_result_succeeded(result, is_error=is_error)
            self.request_response(
                prompt_text("tool_use/continue_success" if succeeded else "tool_use/continue_failure"),
                priority=100,
                reason="tool-result",
                purpose="conversation",
                origin=dict(origin or {}) or None,
            )
        return dict(origin or {})

    def set_result_delivery_handler(self, handler: Callable[[str, str], Any] | None) -> None:
        self._result_delivery_handler = handler

    def set_response_lifecycle_handler(
        self, handler: Callable[[dict[str, Any], str], Any] | None,
    ) -> None:
        self._response_lifecycle_handler = handler

    def set_perception_handlers(
        self,
        context_provider: Callable[[str, datetime], dict[str, Any] | None] | None,
        result_handler: Callable[[dict[str, Any], dict[str, Any]], Any] | None,
    ) -> None:
        self._perception_context_provider = context_provider
        self._perception_result_handler = result_handler

    def set_ambient_context_provider(self, provider) -> None:
        self._ambient_context_provider = provider

    def cancel_observation_questions(self) -> None:
        """Cancel only observation-origin speech, preserving user answers and tasks."""
        with self._response_lock:
            for request in list(self._queued_requests):
                if (request.origin or {}).get("observationReviewId"):
                    self._queued_requests.remove(request)
                    self._return_result_to_inbox(request, "observation-stopped")
            active_ids = {identifier for identifier, request in self._active_response_requests.items()
                          if (request.origin or {}).get("observationReviewId")}
            pending = self._inflight_response
            cancel_pending = bool(pending and (pending.origin or {}).get("observationReviewId"))
        if active_ids:
            self.cancel_response(expected_response_ids=frozenset(active_ids), reason="observation-stopped")
        elif cancel_pending:
            self.cancel_response(reason="observation-stopped")

    async def stop_ambient_observation(self) -> None:
        self.cancel_observation_questions()
        self.last_ambient_check = 0
        await self.ambient_perception.suspend()

    @staticmethod
    def _provider_tool_declarations(declarations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "type": "function",
            "function": {
                "name": str(item["name"]),
                "description": str(item.get("description", "")),
                "parameters": dict(item.get("arguments", {"type": "object", "properties": {}})),
            },
        } for item in declarations]

    def set_tools(
        self,
        declarations: list[dict[str, Any]],
        handler: Callable[..., dict[str, Any]],
    ) -> None:
        self._provider_tools = self._provider_tool_declarations(declarations)
        self._tool_handler = handler
        if self.connected and self.conversation:
            self._configure_dialogue(self.conversation)

    def set_tool_result_handler(
        self,
        handler: Callable[[str, str, Any, bool, dict[str, Any] | None], Any] | None,
    ) -> None:
        self._tool_result_handler = handler

    def set_context_harness(self, harness: ContextHarness) -> None:
        self._context_harness = harness
        entries = harness.journal.entries(types={"tool_call", "tool_result", "tool_execution"})
        finished = {e.get("callId") for e in entries if e.get("type") == "tool_result"}
        calls = {e.get("callId"): e for e in entries if e.get("type") == "tool_call"}
        # A process loss can occur after an external effect but before its receipt.
        # Preserve the unknown outcome; replaying the action could duplicate it.
        for entry in entries:
            if entry.get("type") == "tool_execution" and entry.get("callId") not in finished:
                call = calls.get(entry.get("callId"), {})
                harness.record_tool_result(entry["callId"], call.get("name", "unknown"), {
                    "status": "interrupted", "reason": "Execution ended without a committed result; effect is unconfirmed and was not repeated.",
                }, is_error=True, metadata={"origin": entry.get("origin", {})})
        self._pending_tool_calls = harness.journal.pending_tool_confirmations()
        for entry in harness.journal.entries(tail=512):
            if entry.get("type") == "tool_call" and entry.get("callId"):
                self._remember_tool_call_signature(str(entry["callId"]), (
                    str(entry.get("name", "")),
                    json.dumps(entry.get("arguments", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ))

    def inject_context_data(self, label: str, value: Any) -> bool:
        """Append low-trust structured data without elevating it to instructions."""
        if label == "current_turn_context":
            # Grounded response instructions already carry this turn's evidence.
            # Persisting the same evidence as conversation items every turn
            # accumulates stale scenes in the live socket outside harness budgets.
            self._trace("context.data.request_scoped", {"label": label,
                "turnId": value.get("turnId") if isinstance(value, dict) else None,
                "claimBoundary": "Evidence is supplied in per-response instructions, not appended to persistent conversation."})
            return True
        if not self.connected or not self.conversation:
            return False
        text = (
            f"<{label} trust=\"untrusted-data\">\n"
            + json.dumps(value, ensure_ascii=False, sort_keys=True)
            + f"\n</{label}>"
        )
        self.conversation.create_item({
            "id": "item_" + uuid4().hex,
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        })
        self._trace("context.data.injected", {
            "label": label, "tokensEstimate": max(1, len(text) // 4),
            "content": text, "claimBoundary": "Sent to provider; not an acknowledgement of model use.",
        })
        return True

    def _response_text_delta(self, payload: dict[str, Any]) -> None:
        response_id, request = self._request_for_output(payload)
        if not response_id or not request or request.purpose != "proactive-gate":
            self._trace(
                "response.output.suppressed",
                {"channel": "text", "purpose": request.purpose if request else "unattributed", "reason": "not-structured-gate"},
                response_id=response_id,
            )
            return
        delta = str(payload.get("delta", ""))
        with self._response_lock:
            text_buffers = getattr(self, "_response_text", None)
            if text_buffers is None:
                self._response_text = {}
                text_buffers = self._response_text
            text_buffers[response_id] = text_buffers.get(response_id, "") + delta

    def _response_text_done(self, payload: dict[str, Any]) -> None:
        response_id, request = self._request_for_output(payload)
        if not response_id:
            self._trace("response.output.suppressed", {"channel": "text", "purpose": "unattributed", "reason": "unattributed-response-id"})
            return
        with self._response_lock:
            buffered = getattr(self, "_response_text", {}).pop(response_id, "")
        if not request or request.purpose != "proactive-gate":
            return
        raw_text = str(payload.get("text") or payload.get("transcript") or buffered)
        decision = self.parse_proactive_gate(raw_text)
        self._handle_proactive_decision(decision, response_id=response_id)

    def _handle_proactive_decision(
        self,
        decision: dict[str, Any],
        context: dict[str, Any] | None = None,
        response_id: str | None = None,
    ) -> None:
        """Audit an isolated perception decision and schedule speech on the dialogue lane."""
        self._trace("proactive.gate.decision", decision, response_id=response_id)
        if context is not None:
            if self._perception_result_handler is None:
                self._trace("perception.observation.rejected", {
                    "reason": "no-result-handler",
                    "frameId": context.get("frame_id"),
                    "contextId": context.get("context_id"),
                })
                return
            try:
                handled = self._perception_result_handler(context, decision)
            except Exception as exc:
                self._trace("perception.observation.rejected", {
                    "reason": "result-handler-failed", "error": str(exc)[:240],
                    "frameId": context.get("frame_id"),
                })
                return
            if inspect.isawaitable(handled):
                self.loop.create_task(self._complete_perception_result(handled))
            else:
                self._apply_perception_result(handled)
            return
        if not decision["speak"]:
            return
        reason = decision["reason"]
        speech_policy = "protected" if reason == "hazard" else "finish-clause" if reason in {"path-deviation", "clarification"} else "immediate"
        priority = 90 if reason == "hazard" else 30
        self.request_response(
            f"只自然说出这条必要提醒，不要添加前言、解释或保持安静之类的元话语：{decision['utterance']}",
            priority=priority,
            speech_policy=speech_policy,
            reason=f"proactive-gate:{reason}",
            purpose="proactive-utterance",
        )

    async def _complete_perception_result(self, handled) -> None:
        try:
            self._apply_perception_result(await handled)
        except Exception as exc:
            self._trace("perception.observation.rejected", {
                "reason": "async-result-handler-failed", "error": str(exc)[:240],
            })

    def _apply_perception_result(self, handled: Any) -> None:
        if not isinstance(handled, dict) or not handled.get("accepted"):
            return
        speech = handled.get("speech")
        if not isinstance(speech, dict) or not speech.get("utterance"):
            return
        reason = str(speech.get("reason", "task-evidence"))
        priority = 90 if reason == "hazard" else 35
        self.request_response(
            str(speech["utterance"]),
            priority=priority,
            speech_policy="protected" if reason == "hazard" else "finish-clause",
            reason=f"task-bound-perception:{reason}",
            purpose="proactive-utterance",
            origin=dict(handled.get("origin") or {}),
        )

    @staticmethod
    def parse_perception_output(raw_text: str) -> dict[str, Any]:
        """Parse provider output into evidence only; malformed output fails closed."""
        try:
            return {
                "valid": True,
                **parse_perception_hypothesis(raw_text).model_dump(mode="json"),
            }
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "valid": False,
                "error": str(exc)[:240],
                "checkpoint_id": "unknown",
                "completed": False,
                "confidence": 0.0,
                "summary": "",
                "visible_evidence": [],
                "hazards": [],
                "unrelated": True,
            }

    @staticmethod
    def parse_ambient_output(raw_text: str, *, open_observation=False) -> dict[str, Any]:
        """Parse an ambient observation; malformed output fails closed.

        The fallback is NOT a usable negative observation: callers must reject
        on ``valid=False`` before reading any field. A parse failure means the
        frame was never assessed, which is different from -- and must never be
        recorded as -- evidence that nothing was happening.
        """
        try:
            return {
                "valid": True,
                **parse_ambient_hypothesis(raw_text, open_observation=open_observation).model_dump(mode="json"),
            }
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "valid": False,
                "error": str(exc)[:240],
                "activity_id": NONE_OBSERVED,
                "observed": False,
                "location_id": UNKNOWN_LOCATION,
                "confidence": 0.0,
                "summary": "",
                "visible_evidence": [],
                "hazards": [],
            }

    @staticmethod
    def parse_proactive_gate(raw_text: str) -> dict[str, Any]:
        """Parse a strict decision artifact; malformed or low-value output fails closed."""
        fallback = {
            "speak": False, "utterance": "", "reason": "invalid",
            "urgency": "low", "speech_policy": "immediate", "suppressed": "invalid-json",
            "sceneSummary": "", "salientChange": False,
        }
        try:
            value = json.loads(raw_text.strip())
        except (TypeError, json.JSONDecodeError):
            return fallback
        if not isinstance(value, dict) or not isinstance(value.get("speak"), bool):
            return {**fallback, "suppressed": "invalid-schema"}
        reason = str(value.get("reason", "none"))
        urgency = str(value.get("urgency", "low"))
        utterance = str(value.get("utterance", "")).strip()[:240]
        scene_summary = " ".join(str(value.get("scene_summary", "")).split()).strip()[:500]
        allowed_reason = reason in {"hazard", "path-deviation", "clarification", "timely-help"}
        allowed_urgency = urgency in {"medium", "high"}
        speak = bool(value["speak"] and utterance and allowed_reason and allowed_urgency)
        suppressed = None
        if value["speak"] and not speak:
            suppressed = "policy-gate"
        return {
            "speak": speak, "utterance": utterance if speak else "", "reason": reason,
            "urgency": urgency, "speech_policy": str(value.get("speech_policy", "immediate")),
            "suppressed": suppressed,
            "sceneSummary": scene_summary,
            "salientChange": bool(value.get("salient_change", False)),
        }

    def _schedule_protection_expiry(self, max_protection_ms: int) -> None:
        loop = getattr(self, "loop", None)
        if not loop or not loop.is_running():
            return
        # handle_user_speech_started already holds the scheduler lock.
        response_ids = frozenset(self._active_response_ids)
        generation = getattr(self, "_dialogue_generation", 0)

        def install_timer():
            loop.call_later(max_protection_ms / 1000, self._expire_protection,
                            max_protection_ms, response_ids, generation)
        loop.call_soon_threadsafe(install_timer)

    def _expire_protection(self, max_protection_ms: int = 2500,
                           response_ids: frozenset[str] | None = None, generation: int | None = None) -> None:
        if generation is not None and generation != getattr(self, "_dialogue_generation", 0):
            return
        with self._response_lock:
            protected_ids = frozenset(key for key, item in getattr(self, "_active_response_requests", {}).items()
                                      if item.speech_policy == "protected" and (response_ids is None or key in response_ids))
        if protected_ids and self.cancel_response(drop_queued_below=100, expected_response_ids=protected_ids):
            self._trace("barge_in.protection_expired", {"maxProtectionMs": max_protection_ms})
            self._schedule({"type": "interrupted"})

    def cancel_response(
        self,
        drop_queued_below: int | None = None,
        *,
        reason: str = "explicit-cancel",
        expected_response_ids: frozenset[str] | None = None,
    ) -> bool:
        should_cancel = False
        with self._response_lock:
            if expected_response_ids is not None and not (expected_response_ids & self._active_response_ids):
                return False
            if drop_queued_below is not None:
                to_drop = [r for r in self._queued_requests if r.priority < drop_queued_below]
                for r in to_drop:
                    self._queued_requests.remove(r)
                    self._trace("response.queue.dropped", {"sequence": r.sequence, "threshold": drop_queued_below})
                    self._return_result_to_inbox(r, "queued-response-cancelled")
            if not self._response_pending and not self._active_response_ids:
                return False
            if self._active_response_ids:
                cancelled = getattr(self, "_cancelled_response_ids", None)
                if cancelled is None:
                    cancelled = set()
                    self._cancelled_response_ids = cancelled
                should_cancel = not self._active_response_ids.issubset(cancelled)
                cancelled.update(self._active_response_ids)
            else:
                self._cancel_when_created = True
        if should_cancel:
            self._cancel_provider_response()
        runner = getattr(self, "_tool_runner", None)
        if runner:
            runner.cancel_selection()
        self._trace("response.cancel.requested", {
            "reason": reason,
            "providerSent": should_cancel,
            "cancelWhenCreated": bool(self._cancel_when_created),
        })
        return True

    def _return_result_to_inbox(self, request: _ResponseRequest, reason: str) -> None:
        lifecycle = getattr(self, "_response_lifecycle_handler", None)
        if lifecycle and (request.origin or {}).get("observationReviewId"):
            lifecycle(dict(request.origin), "suspended")
        handler = getattr(self, "_result_delivery_handler", None)
        if handler is None:
            return
        for result_id in self._request_result_ids(request):
            try:
                handler(result_id, "pending")
                self._trace("result.delivery.requeued", {"resultId": result_id, "reason": reason})
            except Exception as exc:
                self._trace("result.delivery.update_failed", {"resultId": result_id, "error": str(exc)})

    @staticmethod
    def _request_result_ids(request):
        origin = request.origin or {} if request else {}
        return list(dict.fromkeys(str(value) for value in [origin.get("resultId"), *origin.get("resultIds", [])] if value))

    def _send_response(self, request: _ResponseRequest) -> None:
        if (getattr(self, "tool_model", None) and self._provider_tools
                and request.purpose == "conversation" and request.reason != "tool-result"
                and (request.origin or {}).get("turnId")):
            turn_id = request.origin["turnId"]
            receipts = [entry for entry in self._context_harness.journal.entries(types={"tool_result", "tool_execution"})
                        if entry.get("type") in {"tool_result", "tool_execution"}
                        and (entry.get("origin") or entry.get("metadata", {}).get("origin", {})).get("turnId") == turn_id]
            if receipts:
                self.inject_context_data("committed_tool_receipts", receipts)
                self._send_response_direct(request)
                return
            from .tool_loop import ToolCallRunner
            if not getattr(self, "_tool_runner", None):
                self._tool_runner = ToolCallRunner(self)
            self._tool_runner.start(request)
            return
        self._send_response_direct(request)

    def _send_response_direct(self, request: _ResponseRequest) -> None:
        permission = getattr(self, "output_permission", None)
        if request.output_mode == "audio" and permission and not permission(request.origin, request.purpose):
            with self._response_lock:
                self._response_pending = False
                self._inflight_response = None
            self._return_result_to_inbox(request, "output-permission")
            self._trace("response.permission.blocked", {"origin": request.origin, "purpose": request.purpose})
            return
        if (getattr(self, "backend", "omni") == "omni"
                and (request.origin or {}).get("inputChannel") == "explicit-control"):
            self.loop.call_soon_threadsafe(self._send_text_response, request)
            return
        try:
            self._response_conversation = self.conversation
            self._trace("response.dispatched", {"sequence": request.sequence, "priority": request.priority, "speechPolicy": request.speech_policy, "outputMode": request.output_mode, "purpose": request.purpose,
                                                "instructions": request.instructions, "origin": dict(request.origin or {})})
            self.conversation.create_response(
                instructions=request.instructions,
                output_modalities=[self.modalities.TEXT] if request.output_mode == "text" else [self.modalities.AUDIO, self.modalities.TEXT],
            )
            self._schedule_response_watchdog(
                "create", request.sequence, getattr(self, "_dialogue_generation", 0),
            )
        except Exception:
            with self._response_lock:
                if self._inflight_response == request:
                    self._response_pending = False
                    self._inflight_response = None
            raise

    def _send_text_response(self, request: _ResponseRequest) -> None:
        try:
            self._start_text_response(request)
        except Exception as exc:
            with self._response_lock:
                if self._inflight_response is not request:
                    return
                self._inflight_response = None
                self._response_pending = False
                self._cancel_when_created = False
            self._trace("response.text_start.failed", {"error": type(exc).__name__, "origin": request.origin})
            self._schedule({"type": "error", "message": "文字回答启动失败，请重试。"})
            lifecycle = getattr(self, "_response_lifecycle_handler", None)
            if lifecycle:
                lifecycle(dict(request.origin or {}), "suspended")
            self._return_result_to_inbox(request, "text-response-start-failed")
            self._schedule_queued_dispatch()

    def _start_text_response(self, request: _ResponseRequest) -> None:
        # An explicit text turn must not wait for an audio commit in Omni.
        # Rebuild this delivery channel from the same durable context and tools.
        if self.closed or self._inflight_response is not request:
            return
        if self._cancel_when_created:
            with self._response_lock:
                self._inflight_response = None
                self._response_pending = False
                self._cancel_when_created = False
            self._return_result_to_inbox(request, "text-response-cancelled-before-start")
            self._schedule_queued_dispatch()
            return
        from .modular import ModularConversation

        conversation = getattr(self, "_text_conversation", None)
        if conversation is None:
            conversation = ModularConversation(
                model=self.model, api_key=self.api_key,
                callback=self._Callback(self._dialogue_generation), text_only=True,
            )
            self._text_conversation = conversation
            conversation.connect()
        from egoassist.memory.grounding import HISTORY_STATEMENT_POLICY
        conversation.update_session(enable_input_audio_transcription=True,
                                    instructions=self.instructions + "\n" + HISTORY_STATEMENT_POLICY)
        conversation.items.clear()
        conversation.item_ids.clear()
        if self._context_harness is not None:
            for item in self._context_harness.provider_replay_items(compact_timestamps=True):
                conversation.create_item(item)
        self._response_conversation = conversation
        self._trace("response.dispatched", {
            "sequence": request.sequence, "purpose": request.purpose, "outputMode": request.output_mode,
            "instructions": request.instructions, "origin": dict(request.origin or {}),
            "backend": "text-and-tts", "model": conversation.model,
        })
        conversation.create_response(
            instructions=request.instructions,
            output_modalities=["text"] if request.output_mode == "text" else ["audio", "text"],
        )

    def _cancel_provider_response(self) -> None:
        conversation = getattr(self, "_response_conversation", None) or self.conversation
        conversation.cancel_response()

    def _schedule_response_watchdog(self, stage: str, identity: int | str, generation: int) -> None:
        loop = getattr(self, "loop", None)
        if not loop or not loop.is_running():
            return
        timeout = (
            getattr(self, "_response_create_timeout", 5.0)
            if stage == "create"
            else getattr(self, "_response_finish_timeout", 45.0)
        )

        def install() -> None:
            loop.call_later(timeout, self._response_watchdog_fired, stage, identity, generation)

        loop.call_soon_threadsafe(install)

    def _response_watchdog_fired(self, stage: str, identity: int | str, generation: int) -> None:
        if self.closed or generation != getattr(self, "_dialogue_generation", 0):
            return
        with self._response_lock:
            if stage == "create":
                request = self._inflight_response
                orphaned = bool(
                    self._response_pending and request and request.sequence == identity
                )
            else:
                orphaned = str(identity) in self._active_response_ids
            queued = self._request_public(self._queued_requests[0]) if self._queued_requests else None
        if not orphaned:
            return
        self._trace("response.watchdog.timeout", {
            "stage": stage,
            "identity": identity,
            "generation": generation,
            "queued": queued,
            "recovery": "dialogue-reconnect",
        }, response_id=str(identity) if stage == "finish" else None)
        self._schedule_reconnect(f"response-{stage}-timeout")

    def _response_created(self, payload: dict[str, Any]) -> None:
        response_id = self._payload_response_id(payload)
        should_cancel = False
        stale: list[tuple[str, _ResponseRequest | None]] = []
        duplicate = False
        terminal_arrived_first = False
        should_dispatch = False
        with self._response_lock:
            if response_id and response_id in self._active_response_ids:
                duplicate = True
                request = self._active_response_requests.get(response_id)
                provider_initiated = False
            else:
                request = self._inflight_response
                provider_initiated = request is None
            if duplicate:
                pass
            else:
                if provider_initiated and response_id and self._active_response_ids:
                    stale = [
                        (item, self._active_response_requests.get(item))
                        for item in self._active_response_ids if item != "provider-active"
                    ]
                    self._active_response_ids.clear()
                    self._active_response_requests.clear()
                    self._cancelled_response_ids.clear()
                    self._speech_started_response_ids.clear()
                self._response_pending = False
                self._inflight_response = None
                if provider_initiated:
                    self._response_sequence += 1
                    request = _ResponseRequest(
                        instructions="", priority=50, sequence=self._response_sequence,
                        reason="provider-auto-user-turn", requested_at=time.monotonic(),
                        output_mode="audio", purpose="conversation",
                    )
                terminal_arrived_first = bool(
                    response_id and response_id in getattr(self, "_early_terminal_response_ids", set())
                )
                if response_id:
                    getattr(self, "_early_terminal_response_ids", set()).discard(response_id)
                if response_id and not terminal_arrived_first:
                    self._active_response_ids.add(response_id)
                    self._active_response_requests[response_id] = request
                    speech_started = getattr(self, "_speech_started_response_ids", set())
                    if "provider-pending" in speech_started:
                        speech_started.discard("provider-pending")
                        speech_started.add(response_id)
                if response_id and request.output_mode == "text" and not terminal_arrived_first:
                    self._response_text[response_id] = ""
                should_cancel = self._cancel_when_created and not terminal_arrived_first
                if should_cancel and response_id:
                    self._cancelled_response_ids.add(response_id)
                self._cancel_when_created = False
                should_dispatch = terminal_arrived_first and bool(self._queued_requests)
        if duplicate:
            self._trace("response.created.duplicate", {}, response_id=response_id)
            return
        for stale_id, stale_request in stale:
            self._persist_response_interruption(stale_id, stale_request, "provider-successor-created")
        if stale:
            self._trace("response.orphan.reconciled", {
                "reason": "provider-successor-created",
                "staleResponseIds": [item[0] for item in stale],
            }, response_id=response_id)
        self._trace(
            "response.created",
            {"sequence": request.sequence, "purpose": request.purpose, "outputMode": request.output_mode,
             "model": (payload.get("response") or {}).get("model"),
             "providerInitiated": provider_initiated, "origin": dict(request.origin or {})},
            response_id=response_id,
        )
        if terminal_arrived_first:
            self._trace("response.orphan.reconciled", {
                "reason": "terminal-arrived-before-created",
            }, response_id=response_id)
            if should_dispatch:
                self._schedule_queued_dispatch()
            return
        if not response_id:
            self._trace("response.created.unattributed", {
                "recovery": "dialogue-reconnect",
            })
            self._schedule_reconnect("response-created-without-id")
            return
        if response_id:
            self._schedule_response_watchdog(
                "finish", response_id, getattr(self, "_dialogue_generation", 0),
            )
        if should_cancel:
            self._cancel_provider_response()

        self._schedule({"type": "response_status", "status": "responding", "responseId": response_id,
                        "origin": request.correlation})

    def _response_finished(self, payload: dict[str, Any]) -> None:
        response_id = self._payload_response_id(payload)
        event_type = str(payload.get("type", "response.done"))
        harness = getattr(self, "_context_harness", None)
        should_dispatch = False
        with self._response_lock:
            known_response = bool(
                response_id and (
                    response_id in self._active_response_ids
                    or "provider-active" in self._active_response_ids
                )
            )
            if response_id and not known_response:
                if self._response_pending:
                    early = getattr(self, "_early_terminal_response_ids", None)
                    if early is None:
                        early = set()
                        self._early_terminal_response_ids = early
                    if len(early) >= 128:
                        early.clear()
                    early.add(response_id)
                    disposition = "buffered-before-created"
                else:
                    disposition = "duplicate-or-unattributed"
            else:
                disposition = None
            if disposition:
                request = None
            else:
                cancelled_ids = getattr(self, "_cancelled_response_ids", set())
                locally_cancelled = bool(response_id and response_id in cancelled_ids)
                if response_id:
                    terminal = getattr(self, "_early_terminal_response_ids", None)
                    if terminal is None:
                        terminal = set()
                        self._early_terminal_response_ids = terminal
                    if len(terminal) >= 128:
                        terminal.clear()
                    terminal.add(response_id)
                    self._active_response_ids.discard(response_id)
                    self._active_response_ids.discard("provider-active")
                else:
                    self._active_response_ids.discard("provider-active")
                request = getattr(self, "_active_response_requests", {}).pop(response_id, None) if response_id else None
                if response_id:
                    getattr(self, "_response_text", {}).pop(response_id, None)
                    cancelled_ids.discard(response_id)
                    getattr(self, "_speech_started_response_ids", set()).discard(response_id)
                should_dispatch = not self._response_pending and not self._active_response_ids and bool(self._queued_requests)
        if disposition:
            self._trace("response.terminal.reconciled", {
                "disposition": disposition, "status": event_type,
            }, response_id=response_id)
            return
        fragment = self._pop_assistant_transcript(response_id)
        purpose = request.purpose if request else "conversation"
        response = payload.get("response")
        provider_status = response.get("status") if isinstance(response, dict) else None
        interrupted = locally_cancelled or event_type in {"response.cancelled", "response.failed"} or provider_status in {"cancelled", "failed", "incomplete"}
        lifecycle_handler = getattr(self, "_response_lifecycle_handler", None)
        if request and request.origin and lifecycle_handler:
            try:
                lifecycle_handler(
                    {**dict(request.origin), "responseId": response_id,
                     **({"audioAvailable": response["audioAvailable"]}
                        if isinstance(response, dict) and isinstance(response.get("audioAvailable"), bool) else {})},
                    "suspended" if interrupted else "completed",
                )
            except Exception as exc:
                self._trace("intent.lifecycle.update_failed", {"error": str(exc)})
        delivery_handler = getattr(self, "_result_delivery_handler", None)
        if delivery_handler:
            for result_id in self._request_result_ids(request):
                try:
                    delivery_handler(result_id, "pending" if interrupted else "delivered")
                except Exception as exc:
                    self._trace("result.delivery.update_failed", {"resultId": result_id, "error": str(exc)})
        if fragment:
            self._last_assistant_utterance = fragment
            self._last_assistant_utterance_at = time.monotonic()
            if harness and purpose in {"conversation", "proactive-utterance"}:
                harness.record_assistant(
                    fragment,
                    response_id=response_id,
                    status="interrupted" if interrupted else "completed",
                    purpose=purpose,
                )
        if interrupted and harness:
            interruption_reason = event_type if not locally_cancelled else f"local-cancel:{event_type}"
            harness.record_interruption(response_id, reason=interruption_reason)
        usage = self._response_usage(payload)
        if usage and harness:
            nested = payload.get("response", {})
            model = nested.get("model") if isinstance(nested, dict) else payload.get("model")
            harness.record_provider_usage(
                response_id,
                usage,
                status=event_type.removeprefix("response."),
                model=str(model) if model else None,
                context_scope="request" if str(response_id).startswith("modular-") else "persistent-session",
            )
        self._trace(
            "response.finished",
            {
                "sequence": request.sequence if request else None,
                "status": payload.get("type", "done"),
                "usage": usage,
                "fragmentCommitted": bool(fragment),
                "interrupted": interrupted,
                "localCancelRequested": locally_cancelled,
                "providerStatus": provider_status,
                "model": response.get("model") if isinstance(response, dict) else None,
                "origin": dict(request.origin or {}) if request else {},
            },
            response_id=response_id or None,
        )
        self._schedule({"type": "response_status", "status": "interrupted" if interrupted else "completed",
                        "responseId": response_id, "origin": request.correlation if request else {},
                        "providerStatus": provider_status})
        # Track completed exchanges so refresh_harness_context() can trigger
        # a proactive compact+reconnect when DashScope's live context grows too large.
        if not interrupted and not str(response_id).startswith("modular-"):
            self._dialogue_exchanges = getattr(self, "_dialogue_exchanges", 0) + 1
        if should_dispatch:
            self._schedule_queued_dispatch()

    def _pop_assistant_transcript(self, response_id: str | None) -> str:
        key = response_id or "__unattributed__"
        return str(self._assistant_transcript_buffers.pop(key, ""))

    @staticmethod
    def _response_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
        nested = payload.get("response", {})
        nested_usage = nested.get("usage") if isinstance(nested, dict) else None
        raw = nested_usage if isinstance(nested_usage, dict) else payload.get("usage")
        if not isinstance(raw, dict):
            return None

        def token_value(*names: str) -> int | None:
            for name in names:
                value = raw.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return None

        input_tokens = token_value("input_tokens", "prompt_tokens", "inputTokens")
        output_tokens = token_value("output_tokens", "completion_tokens", "outputTokens")
        total_tokens = token_value("total_tokens", "totalTokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        if input_tokens is None and output_tokens is None and total_tokens is None:
            return None
        usage = {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
        }
        for source, target in (
            ("input_token_details", "inputTokenDetails"),
            ("input_tokens_details", "inputTokenDetails"),
            ("output_token_details", "outputTokenDetails"),
            ("output_tokens_details", "outputTokenDetails"),
        ):
            if source in raw and target not in usage:
                usage[target] = raw[source]
        return usage

    def _persist_active_interruptions(self, reason: str) -> None:
        harness = getattr(self, "_context_harness", None)
        if harness is None:
            return
        with self._response_lock:
            active = [
                (response_id, getattr(self, "_active_response_requests", {}).get(response_id))
                for response_id in self._active_response_ids
                if response_id != "provider-active"
            ]
        for response_id, request in active:
            self._persist_response_interruption(response_id, request, reason)

    def _persist_response_interruption(
        self,
        response_id: str,
        request: _ResponseRequest | None,
        reason: str,
    ) -> None:
        harness = getattr(self, "_context_harness", None)
        if harness is None:
            return
        fragment = self._pop_assistant_transcript(response_id)
        purpose = request.purpose if request else "conversation"
        if fragment and purpose in {"conversation", "proactive-utterance"}:
            harness.record_assistant(
                fragment,
                response_id=response_id,
                status="interrupted",
                purpose=purpose,
            )
        harness.record_interruption(response_id, reason=reason)

    def _response_conflict(self) -> None:
        with self._response_lock:
            inflight = self._inflight_response
            self._response_pending = False
            self._inflight_response = None
            self._cancel_when_created = False
            if inflight and inflight not in self._queued_requests:
                self._queued_requests.appendleft(inflight)  # restore to front of queue
            if not self._active_response_ids:
                self._active_response_ids.add("provider-active")
        self._trace("response.conflict.requeued", {"sequence": inflight.sequence if inflight else None})
        self._schedule_response_watchdog(
            "finish", "provider-active", getattr(self, "_dialogue_generation", 0),
        )

    def _response_absent_after_cancel(self) -> None:
        """Reconcile a benign cancel/done race reported by the provider."""
        should_dispatch = False
        with self._response_lock:
            stale_ids = sorted(self._active_response_ids)
            self._active_response_ids.clear()
            getattr(self, "_active_response_requests", {}).clear()
            getattr(self, "_cancelled_response_ids", set()).clear()
            getattr(self, "_speech_started_response_ids", set()).clear()
            self._cancel_when_created = False
            self._barge_in_pending = False
            should_dispatch = not self._response_pending and bool(self._queued_requests)
        self._trace("response.cancel.already_finished", {"staleResponseIds": stale_ids})
        if should_dispatch:
            self._schedule_queued_dispatch()

    def _schedule_queued_dispatch(self) -> None:
        loop = getattr(self, "loop", None)
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._dispatch_queued)
        else:
            self._dispatch_queued()

    def _dispatch_queued(self) -> None:
        with self._response_lock:
            if self._response_pending or self._active_response_ids or not self._queued_requests:
                return
            request = self._queued_requests.popleft()
            self._response_pending = True
            self._inflight_response = request
        self._send_response(request)

    def update_context(self, instructions: str) -> None:
        self.instructions = instructions
        if not self.connected or not self.conversation:
            return
        self._configure_dialogue(self.conversation)

    def apply_context_projection(self, instructions: str, manifest: dict[str, Any]) -> None:
        """Apply system context and replace a stale provider-side history projection."""
        self.instructions = instructions
        compaction_id = manifest.get("latestCompactionId")
        replayed_id = getattr(self, "_replayed_compaction_id", None)
        if self.connected and compaction_id and compaction_id != replayed_id:
            self.connected = False
            self._trace("context.projection.stale", {
                "replayedCompactionId": replayed_id,
                "requiredCompactionId": compaction_id,
                "contextHash": manifest.get("contextHash"),
            })
            self._schedule_reconnect("context-compacted")
            return
        self.update_context(instructions)

    def set_trace_sink(self, sink: Callable[..., Any]) -> None:
        self._trace_sink = sink

    def scheduler_snapshot(self) -> dict[str, Any]:
        with self._response_lock:
            active = list(getattr(self, "_active_response_requests", {}).values())
            return {
                "pending": self._response_pending,
                "activeResponseIds": sorted(self._active_response_ids),
                "queued": [self._request_public(r) for r in self._queued_requests] or None,
                "inflight": self._request_public(self._inflight_response),
                "activePolicies": [self._request_public(item) for item in active],
                "bargeInPending": bool(getattr(self, "_barge_in_pending", False)),
            }

    def provider_lanes_snapshot(self) -> dict[str, Any]:
        return {
            "dialogue": {
                "backend": getattr(self, "backend", "omni"),
                "models": getattr(self, "modular_config", None),
                "connected": self.connected,
                "providerError": self._blocked_failure.public() if getattr(self, "_blocked_failure", None) else None,
                "microphone": True,
                "cameraContext": "question-bound-visual-evidence" if getattr(self, "backend", "omni") == "modular" else True,
                "turnDetection": "webrtc-vad-segment-asr" if getattr(self, "backend", "omni") == "modular" else "server-vad-explicit-response",
                "userVisibleOutput": True,
            },
            "perception": self.perception.snapshot(),
            "ambient": self.ambient_perception.snapshot() if getattr(self, "ambient_perception", None) else None,
        }

    @staticmethod
    def _request_public(request: _ResponseRequest | None) -> dict[str, Any] | None:
        if not request:
            return None
        return {
            "sequence": request.sequence,
            "priority": request.priority,
            "speechPolicy": request.speech_policy,
            "reason": request.reason,
            "maxProtectionMs": request.max_protection_ms,
            "outputMode": request.output_mode,
            "purpose": request.purpose,
            "origin": dict(request.origin or {}),
        }

    def _trace(self, event_type: str, payload: dict[str, Any], response_id: str | None = None) -> dict[str, Any]:
        sink = getattr(self, "_trace_sink", None)
        if not sink:
            return {}
        return sink(event_type, "realtime-bridge", payload, [], response_id=response_id)

    async def close(self) -> None:
        self._persist_active_interruptions("bridge-closed")
        self.closed = True
        self.connected = False
        self._drop_open_input_segments("bridge-closed")
        self._dialogue_generation += 1
        text_conversation = getattr(self, "_text_conversation", None)
        if text_conversation is not None:
            await text_conversation.aclose()
            self._text_conversation = None
        if getattr(self, "_tool_runner", None):
            await self._tool_runner.close()
        if getattr(self, "tool_model", None):
            await self.tool_model.close()
        await self.perception.close()
        if getattr(self, "ambient_perception", None):
            await self.ambient_perception.close()
        if self.conversation:
            try:
                await asyncio.to_thread(self.conversation.close)
            except Exception:
                pass  # ws=None: DashScope connection was never established or already closed

    def _schedule(self, payload: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(self.emit(payload), self.loop)

    def _schedule_callback(self, callback, *args) -> None:
        self.loop.call_soon_threadsafe(callback, *args)
