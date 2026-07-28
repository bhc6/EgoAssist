"""Qwen Omni realtime bridge for browser audio/video streams."""

from __future__ import annotations

import asyncio
import base64
import difflib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests


_DNS_LOCK = threading.Lock()
_DASHSCOPE_HOST = "dashscope.aliyuncs.com"
LOGGER = logging.getLogger(__name__)


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


def _realtime_public_ips(host: str = _DASHSCOPE_HOST) -> list[str]:
    override = os.getenv("DASHSCOPE_REALTIME_IPS", "")
    if override:
        return [item.strip() for item in override.split(",") if item.strip()]
    system_ips = sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    if system_ips and not all(_is_fake_ip(item) for item in system_ips):
        return []
    response = requests.get(
        "https://dns.alidns.com/resolve",
        params={"name": host, "type": "A"}, timeout=10,
    )
    response.raise_for_status()
    return list(dict.fromkeys(
        item["data"] for item in response.json().get("Answer", [])
        if item.get("type") == 1 and not _is_fake_ip(str(item.get("data", "")))
    ))


def _connect_with_resolved_ip(conversation, host: str = _DASHSCOPE_HOST) -> None:
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
                errors.append(f"{public_ip}: {exc}")
                if getattr(conversation, "ws", None):
                    conversation.close()
            finally:
                socket.getaddrinfo = original
    raise ConnectionError("DashScope realtime connection failed via public DNS: " + "; ".join(errors))


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
        from dashscope.audio.qwen_omni import (
            MultiModality,
            OmniRealtimeCallback,
            OmniRealtimeConversation,
        )

        dashscope.api_key = api_key
        self.loop = asyncio.get_running_loop()
        self.emit = emit
        self.on_transcript = on_transcript
        self.connecting = True
        self._response_lock = threading.Lock()
        self._response_pending = False
        self._active_response_ids: set[str] = set()
        self._active_response_requests: dict[str, _ResponseRequest] = {}
        self._queued_response: _ResponseRequest | None = None
        self._inflight_response: _ResponseRequest | None = None
        self._response_sequence = 0
        self._cancel_when_created = False
        self._barge_in_pending = False
        self._output_channels_seen: set[tuple[str, str]] = set()
        self._response_text: dict[str, str] = {}
        self._assistant_transcript_buffer = ""
        self._last_assistant_utterance = ""
        self._last_assistant_utterance_at = 0.0
        self._trace_sink: Callable[..., Any] | None = None
        self._media_lock = threading.Lock()
        self._pending_video: str | None = None
        bridge = self

        class Callback(OmniRealtimeCallback):
            def on_open(self):
                bridge._schedule({"type": "ready"})

            def on_close(self, close_status_code=None, close_msg=None):
                if not bridge.connecting:
                    bridge._schedule({"type": "closed", "message": str(close_msg or "")})

            def on_event(self, response):
                event_type = response.get("type", "")
                if event_type == "response.created":
                    bridge._response_created(response)
                elif event_type in {"response.done", "response.cancelled", "response.failed"}:
                    bridge._response_finished(response)
                if event_type == "response.audio.delta":
                    bridge.note_output("audio", response)
                    bridge._schedule({"type": "audio", "audio": response.get("delta", "")})
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = response.get("transcript", "")
                    attribution = bridge.attribute_input_transcript(transcript)
                    bridge._trace("audio.transcript.attributed", attribution)
                    bridge._schedule({
                        "type": "user_transcript",
                        "speaker": "user",
                        "channel": "input_audio_transcription",
                        "echoRisk": attribution["echoRisk"],
                        "text": transcript,
                    })
                    bridge._schedule_callback(bridge.on_transcript, transcript)
                elif event_type in {"response.audio_transcript.delta", "response.audio_transcript.done"}:
                    transcript_part = response.get("delta") or response.get("transcript", "")
                    bridge.note_output("transcript", response)
                    bridge._schedule({
                        "type": "assistant_transcript",
                        "speaker": "assistant",
                        "channel": "spoken_audio",
                        "text": transcript_part,
                        "done": event_type.endswith(".done"),
                    })
                    bridge.note_assistant_transcript(transcript_part, event_type.endswith(".done"))
                elif event_type == "response.text.delta":
                    bridge.note_output("text", response)
                    bridge._response_text_delta(response)
                elif event_type == "response.text.done":
                    bridge._response_text_done(response)
                elif event_type == "input_audio_buffer.speech_started":
                    barge_action = bridge.handle_user_speech_started()
                    if barge_action == "cancelled":
                        bridge._schedule({"type": "interrupted"})
                    elif barge_action in {"deferred", "protected"}:
                        bridge._schedule({"type": "barge_in_policy", "action": barge_action})
                elif event_type == "error":
                    if _is_active_response_conflict(response):
                        LOGGER.info("DashScope response backpressure; requeueing inflight request")
                        bridge._response_conflict()
                    elif _is_no_active_response(response):
                        LOGGER.info("DashScope cancel/done race; reconciling stale response state")
                        bridge._response_absent_after_cancel()
                    else:
                        LOGGER.error("DashScope realtime error: %s", response)
                        bridge._schedule({"type": "error", "message": str(response)})

        self.conversation = OmniRealtimeConversation(
            model=model,
            callback=Callback(),
            api_key=api_key,
            url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
        )
        self.modalities = MultiModality
        self.voice = voice
        self.instructions = instructions
        self.last_proactive_check = 0.0

    async def connect(self) -> None:
        await asyncio.to_thread(_connect_with_resolved_ip, self.conversation)
        self.connecting = False
        self.conversation.update_session(
            output_modalities=[self.modalities.AUDIO, self.modalities.TEXT],
            voice=self.voice,
            instructions=self.instructions,
            enable_input_audio_transcription=True,
            enable_turn_detection=True,
            turn_detection_type="server_vad",
            turn_detection_threshold=0.2,
            turn_detection_silence_duration_ms=650,
        )

    def append_audio(self, audio_b64: str) -> bool:
        if not audio_b64:
            return False
        video_flushed = False
        with self._media_lock:
            self.conversation.append_audio(audio_b64)
            if self._pending_video:
                self.conversation.append_video(self._pending_video)
                self._pending_video = None
                video_flushed = True
        return video_flushed

    def append_video(self, image_b64: str) -> bool:
        if not image_b64:
            return False
        with self._media_lock:
            # The official camera sample sends a frame only from the microphone
            # loop, immediately after a fresh audio chunk. A prior audio append
            # may already have been auto-committed by server VAD.
            self._pending_video = image_b64
            return False

    def maybe_check_proactively(self, interval_seconds: float = 4.0) -> None:
        now = time.monotonic()
        with self._response_lock:
            if now - self.last_proactive_check < interval_seconds:
                return
            self.last_proactive_check = now
        self.request_response(
            "检查最近画面，只返回一个 JSON 对象，不要输出 JSON 之外的内容："
            '{"speak":false,"utterance":"","reason":"none","urgency":"low","speech_policy":"immediate",'
            '"scene_summary":"客观描述当前可见活动，不推断身份或意图","salient_change":false}。'
            "仅在明显风险、需要用户确认的路径偏离，或一句短提示现在确有帮助时令 speak=true；"
            "reason 只能是 hazard、path-deviation、clarification、timely-help 或 none；speak=true 时 urgency 必须是 medium 或 high。"
            "utterance 必须是用户现在应该听到的一句短话。正确进行、证据不足或只是想表示保持安静时必须 speak=false。"
            "不要假设步骤已经完成。",
            priority=10,
            output_mode="text",
            purpose="proactive-gate",
            reason="periodic-visual-intervention-gate",
        )

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
    ) -> bool:
        """Request one response, coalescing work while the provider is busy."""
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
            )
            self._trace("response.requested", {"sequence": request.sequence, "priority": priority, "speechPolicy": speech_policy, "reason": reason, "outputMode": output_mode, "purpose": purpose})
            busy = self._response_pending or bool(self._active_response_ids)
            if busy:
                queued = self._queued_response
                if queued is None or (request.priority, request.sequence) >= (queued.priority, queued.sequence):
                    self._queued_response = request
                    self._trace("response.queued", {"sequence": request.sequence, "replacedSequence": queued.sequence if queued else None})
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
                self.conversation.cancel_response()
                self._trace("response.cancel.requested", {"reason": "higher-priority-request"})
            except Exception:
                pass
        if should_dispatch:
            self._send_response(request)
        return should_dispatch

    def handle_user_speech_started(self) -> str:
        """Apply an explicit barge-in policy while continuing microphone capture."""
        with self._response_lock:
            requests = list(getattr(self, "_active_response_requests", {}).values())
            current = max(requests, key=lambda item: item.sequence) if requests else None
            policy = current.speech_policy if current else "immediate"
            reason = current.reason if current else "provider-or-untracked-response"
            if policy == "protected":
                self._trace("barge_in.protected", {"policy": policy, "reason": reason, "maxProtectionMs": current.max_protection_ms})
                self._schedule_protection_expiry(current.max_protection_ms)
                return "protected"
            if policy == "finish-clause":
                self._barge_in_pending = True
                self._trace("barge_in.deferred", {"policy": policy, "reason": reason})
                return "deferred"
        if self.cancel_response(drop_queued_below=100):
            self._trace("barge_in.cancelled", {"policy": "immediate", "reason": reason})
            return "cancelled"
        return "idle"

    def note_assistant_transcript(self, text: str, done: bool = False) -> None:
        if done:
            completed = text or getattr(self, "_assistant_transcript_buffer", "")
            self._last_assistant_utterance = completed
            self._last_assistant_utterance_at = time.monotonic()
            self._assistant_transcript_buffer = ""
        else:
            self._assistant_transcript_buffer = getattr(self, "_assistant_transcript_buffer", "") + text
        with self._response_lock:
            should_cancel = bool(getattr(self, "_barge_in_pending", False) and (done or any(mark in text for mark in "。！？.!?；;")))
            if should_cancel:
                self._barge_in_pending = False
        if should_cancel and self.cancel_response(drop_queued_below=100):
            self._trace("barge_in.clause_boundary", {"boundaryText": text[-40:]})
            self._schedule({"type": "interrupted"})

    def attribute_input_transcript(self, text: str) -> dict[str, Any]:
        """Mark likely acoustic echo without suppressing possible user repetition."""
        normalize = lambda value: re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())
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
            "speaker": "user",
            "sourceChannel": "input_audio_transcription",
            "echoRisk": "suspected" if similarity >= 0.82 else "low",
            "assistantSimilarity": round(similarity, 4),
            "assistantAgeMs": round(age_ms, 2) if utterance_at else None,
            "policy": "observe-only-never-drop",
        }

    def note_output(self, channel: str, payload: dict[str, Any]) -> None:
        response_id = str(payload.get("response_id") or payload.get("response", {}).get("id") or "active")
        key = (response_id, channel)
        seen = getattr(self, "_output_channels_seen", set())
        if key in seen:
            return
        seen.add(key)
        self._output_channels_seen = seen
        request = next(iter(getattr(self, "_active_response_requests", {}).values()), None)
        latency_ms = (time.monotonic() - request.requested_at) * 1000 if request and request.requested_at else None
        sink = getattr(self, "_trace_sink", None)
        if sink:
            sink(
                f"response.first_{channel}", "realtime-provider",
                {"channel": channel, "sequence": request.sequence if request else None}, [],
                response_id=response_id, latency_ms=latency_ms,
            )

    def _current_response_id(self, payload: dict[str, Any]) -> str:
        response_id = str(payload.get("response_id") or payload.get("response", {}).get("id") or "")
        if response_id:
            return response_id
        with self._response_lock:
            return next(reversed(sorted(self._active_response_ids)), "provider-active")

    def _response_text_delta(self, payload: dict[str, Any]) -> None:
        response_id = self._current_response_id(payload)
        delta = str(payload.get("delta", ""))
        with self._response_lock:
            text_buffers = getattr(self, "_response_text", None)
            if text_buffers is None:
                self._response_text = {}
                text_buffers = self._response_text
            text_buffers[response_id] = text_buffers.get(response_id, "") + delta

    def _response_text_done(self, payload: dict[str, Any]) -> None:
        response_id = self._current_response_id(payload)
        with self._response_lock:
            request = getattr(self, "_active_response_requests", {}).get(response_id)
            buffered = getattr(self, "_response_text", {}).pop(response_id, "")
        if not request or request.purpose != "proactive-gate":
            return
        raw_text = str(payload.get("text") or payload.get("transcript") or buffered)
        decision = self.parse_proactive_gate(raw_text)
        self._trace("proactive.gate.decision", decision, response_id=response_id)
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
        meta_silence = any(marker in utterance.casefold() for marker in (
            "保持安静", "不主动输出", "不会主动", "不打扰", "无需回复", "我会安静",
            "stay quiet", "remain silent", "won't interrupt",
        ))
        speak = bool(value["speak"] and utterance and allowed_reason and allowed_urgency and not meta_silence)
        suppressed = None
        if value["speak"] and not speak:
            suppressed = "meta-silence" if meta_silence else "policy-gate"
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
        def install_timer():
            loop.call_later(max_protection_ms / 1000, self._expire_protection, max_protection_ms)
        loop.call_soon_threadsafe(install_timer)

    def _expire_protection(self, max_protection_ms: int = 2500) -> None:
        with self._response_lock:
            protected = any(item.speech_policy == "protected" for item in getattr(self, "_active_response_requests", {}).values())
        if protected and self.cancel_response(drop_queued_below=100):
            self._trace("barge_in.protection_expired", {"maxProtectionMs": max_protection_ms})
            self._schedule({"type": "interrupted"})

    def cancel_response(self, drop_queued_below: int | None = None) -> bool:
        should_cancel = False
        with self._response_lock:
            if drop_queued_below is not None and self._queued_response and self._queued_response.priority < drop_queued_below:
                self._trace("response.queue.dropped", {"sequence": self._queued_response.sequence, "threshold": drop_queued_below})
                self._queued_response = None
            if not self._response_pending and not self._active_response_ids:
                return False
            if self._active_response_ids:
                should_cancel = True
            else:
                self._cancel_when_created = True
        if should_cancel:
            self.conversation.cancel_response()
        return True

    def _send_response(self, request: _ResponseRequest) -> None:
        try:
            self._trace("response.dispatched", {"sequence": request.sequence, "priority": request.priority, "speechPolicy": request.speech_policy, "outputMode": request.output_mode, "purpose": request.purpose})
            self.conversation.create_response(
                instructions=request.instructions,
                output_modalities=[self.modalities.TEXT] if request.output_mode == "text" else [self.modalities.AUDIO, self.modalities.TEXT],
            )
        except Exception:
            with self._response_lock:
                if self._inflight_response == request:
                    self._response_pending = False
                    self._inflight_response = None
            raise

    def _response_created(self, payload: dict[str, Any]) -> None:
        response_id = str(payload.get("response", {}).get("id", "provider-active"))
        should_cancel = False
        with self._response_lock:
            request = self._inflight_response
            self._response_pending = False
            self._inflight_response = None
            self._active_response_ids.add(response_id)
            if request:
                if not hasattr(self, "_active_response_requests"):
                    self._active_response_requests = {}
                self._active_response_requests[response_id] = request
            if request and request.output_mode == "text":
                if not hasattr(self, "_response_text"):
                    self._response_text = {}
                self._response_text[response_id] = ""
            should_cancel = self._cancel_when_created
            self._cancel_when_created = False
        self._trace("response.created", {"sequence": request.sequence if request else None}, response_id=response_id)
        if should_cancel:
            self.conversation.cancel_response()

    def _response_finished(self, payload: dict[str, Any]) -> None:
        response_id = str(payload.get("response", {}).get("id", ""))
        should_dispatch = False
        with self._response_lock:
            if response_id:
                self._active_response_ids.discard(response_id)
                self._active_response_ids.discard("provider-active")
            else:
                self._active_response_ids.discard("provider-active")
            request = getattr(self, "_active_response_requests", {}).pop(response_id, None)
            getattr(self, "_response_text", {}).pop(response_id, None)
            should_dispatch = not self._response_pending and not self._active_response_ids and self._queued_response is not None
        self._trace(
            "response.finished",
            {"sequence": request.sequence if request else None, "status": payload.get("type", "done")},
            response_id=response_id or None,
        )
        if should_dispatch:
            self._schedule_queued_dispatch()

    def _response_conflict(self) -> None:
        with self._response_lock:
            inflight = self._inflight_response
            self._response_pending = False
            self._inflight_response = None
            self._cancel_when_created = False
            if inflight and (
                self._queued_response is None
                or (inflight.priority, inflight.sequence) >= (self._queued_response.priority, self._queued_response.sequence)
            ):
                self._queued_response = inflight
            if not self._active_response_ids:
                self._active_response_ids.add("provider-active")
        self._trace("response.conflict.requeued", {"sequence": inflight.sequence if inflight else None})

    def _response_absent_after_cancel(self) -> None:
        """Reconcile a benign cancel/done race reported by the provider."""
        should_dispatch = False
        with self._response_lock:
            stale_ids = sorted(self._active_response_ids)
            self._active_response_ids.clear()
            getattr(self, "_active_response_requests", {}).clear()
            self._cancel_when_created = False
            self._barge_in_pending = False
            should_dispatch = not self._response_pending and self._queued_response is not None
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
            if self._response_pending or self._active_response_ids or self._queued_response is None:
                return
            request = self._queued_response
            self._queued_response = None
            self._response_pending = True
            self._inflight_response = request
        self._send_response(request)

    def update_context(self, instructions: str) -> None:
        self.instructions = instructions
        self.conversation.update_session(
            output_modalities=[self.modalities.AUDIO, self.modalities.TEXT],
            voice=self.voice,
            instructions=instructions,
        )

    def set_trace_sink(self, sink: Callable[..., Any]) -> None:
        self._trace_sink = sink

    def scheduler_snapshot(self) -> dict[str, Any]:
        with self._response_lock:
            active = list(getattr(self, "_active_response_requests", {}).values())
            return {
                "pending": self._response_pending,
                "activeResponseIds": sorted(self._active_response_ids),
                "queued": self._request_public(self._queued_response),
                "inflight": self._request_public(self._inflight_response),
                "activePolicies": [self._request_public(item) for item in active],
                "bargeInPending": bool(getattr(self, "_barge_in_pending", False)),
            }

    @staticmethod
    def _request_public(request: _ResponseRequest | None) -> dict[str, Any] | None:
        if not request:
            return None
        return {"sequence": request.sequence, "priority": request.priority, "speechPolicy": request.speech_policy, "reason": request.reason, "maxProtectionMs": request.max_protection_ms, "outputMode": request.output_mode, "purpose": request.purpose}

    def _trace(self, event_type: str, payload: dict[str, Any], response_id: str | None = None) -> dict[str, Any]:
        sink = getattr(self, "_trace_sink", None)
        if not sink:
            return {}
        return sink(event_type, "realtime-bridge", payload, [], response_id=response_id)

    async def close(self) -> None:
        await asyncio.to_thread(self.conversation.close)

    def _schedule(self, payload: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(self.emit(payload), self.loop)

    def _schedule_callback(self, callback, *args) -> None:
        self.loop.call_soon_threadsafe(callback, *args)


def pcm16_bytes_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
