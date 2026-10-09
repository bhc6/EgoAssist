"""ASR, text/VL and streaming TTS behind the existing response lifecycle.

The conversation interface translates provider events only. Permissions, tools,
memory, interruption ownership and durable replay stay in the shared bridge.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import aclosing
from copy import deepcopy
import io
import json
import os
import time
from uuid import uuid4
import wave

import aiohttp

from egoassist.core.settings import integer, number

TTS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"


class Modalities:
    AUDIO = "audio"
    TEXT = "text"


class Callback:
    def on_open(self): pass
    def on_close(self, *args): pass
    def on_event(self, event): pass


def settings():
    return {
        "textModel": os.getenv("EGOASSIST_MODULAR_TEXT_MODEL", "qwen-flash"),
        "visionModel": os.getenv("EGOASSIST_VISUAL_QUESTION_MODEL", "qwen3-vl-flash"),
        "asrModel": os.getenv("EGOASSIST_ASR_MODEL", "qwen3-asr-flash"),
        "ttsModel": os.getenv("EGOASSIST_MODULAR_TTS_MODEL", "qwen3-tts-flash-realtime"),
        "voice": os.getenv("EGOASSIST_MODULAR_VOICE", "Cherry"),
        "timeout": number("EGOASSIST_MODULAR_TIMEOUT", 45),
        "asrTimeout": number("EGOASSIST_ASR_TIMEOUT", 10),
        "maxAudioSeconds": integer("EGOASSIST_MODULAR_MAX_AUDIO_SECONDS", 20, minimum=1, maximum=30),
        "asrQueue": integer("EGOASSIST_MODULAR_ASR_QUEUE", 4, maximum=16),
        "historyChars": integer("EGOASSIST_MODULAR_HISTORY_CHARS", 48000, minimum=1024, maximum=256000),
        "ttsPrefetch": bool(integer("EGOASSIST_TTS_PREFETCH", 1, minimum=0, maximum=1)),
    }


def message_text(content):
    if isinstance(content, str):
        return content
    return "".join(item.get("text", "") for item in content or [] if isinstance(item, dict))


class SpeechSegments:
    """Bounded PCM16/16kHz framing with WebRTC VAD and 300ms pre-roll."""
    frame_bytes = 640

    def __init__(self, on_start, on_end, *, max_seconds=20, vad=None):
        if vad is None:
            try:
                import webrtcvad
            except ImportError as exc:
                raise RuntimeError("Modular audio requires pip install -e '.[modular]'") from exc
            vad = webrtcvad.Vad(3)
        self.vad, self.on_start, self.on_end = vad, on_start, on_end
        self.max_frames = max_seconds * 50
        self.pending = bytearray()
        self.preroll = deque(maxlen=15)
        self.frames = []
        self.speech_run = self.silence_run = self.samples = 0
        self.segment_id = None

    def feed(self, pcm):
        if len(pcm) % 2:
            raise ValueError("PCM16 input must have complete samples")
        self.pending.extend(pcm)
        while len(self.pending) >= self.frame_bytes:
            frame = bytes(self.pending[:self.frame_bytes])
            del self.pending[:self.frame_bytes]
            self.samples += 320
            speech = self.vad.is_speech(frame, 16000)
            if self.segment_id is None:
                self.preroll.append(frame)
                self.speech_run = self.speech_run + 1 if speech else 0
                # A 60 ms impulse was enough to trigger an ASR request on
                # background noise. Require sustained speech; keep pre-roll
                # so the first phoneme is not lost when onset is confirmed.
                if self.speech_run >= 6:
                    self.segment_id = "asr-" + uuid4().hex
                    self.frames = list(self.preroll)
                    self.preroll.clear()
                    self.on_start(self.segment_id, (self.samples - len(self.frames) * 320) // 16)
            else:
                self.frames.append(frame)
                self.silence_run = 0 if speech else self.silence_run + 1
                if self.silence_run >= 33 or len(self.frames) >= self.max_frames:
                    sid, pcm = self.segment_id, b"".join(self.frames)
                    self.segment_id = None
                    self.frames = []
                    self.silence_run = self.speech_run = 0
                    self.on_end(sid, self.samples // 16, pcm)


class ModularProvider:
    def __init__(self, api_key, config, audit):
        self.api_key, self.config, self.audit = api_key, config, audit
        self.session = None

    def audit_request(self, stage, model, request):
        call_id = uuid4().hex
        self.audit({'auditEvent': 'request', 'callId': call_id, 'stage': stage,
                    'model': model, 'request': deepcopy(request)})
        return call_id

    def http(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"Authorization": "Bearer " + self.api_key}, trust_env=True,
                timeout=aiohttp.ClientTimeout(total=self.config["timeout"]), read_bufsize=1024 * 1024)
        return self.session

    @staticmethod
    def url(endpoint):
        import dashscope
        return dashscope.base_http_api_url.rstrip("/") + endpoint

    async def _json(self, stage, model, body, timeout):
        started = time.perf_counter()
        result = {"stage": stage, "model": model, "apiCalls": 1, "cacheHits": 0, "cost": None}
        call_id = self.audit_request(stage, model, {"model": model, **body})
        payload = None
        try:
            async with self.http().post(self.url("/services/aigc/multimodal-generation/generation"),
                                        json={"model": model, **body}, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                response.raise_for_status()
                payload = await response.json()
            result.update(status="completed", usage=payload.get("usage"), requestId=payload.get("request_id"))
            choice = payload["output"]["choices"][0]
            if choice.get("finish_reason") not in (None, "stop"):
                raise ValueError("incomplete provider output")
            return message_text(choice["message"]["content"])
        except BaseException as exc:
            result.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=type(exc).__name__)
            raise
        finally:
            self.audit({'auditEvent': 'response', 'callId': call_id, **result, 'output': payload,
                        'latencyMs': (time.perf_counter() - started) * 1000})
            self.audit({**result, "latencyMs": (time.perf_counter() - started) * 1000})

    async def transcribe(self, pcm):
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        return await self._json("asr", self.config["asrModel"], {
            "input": {"messages": [{"role": "user", "content": [
                {"audio": "data:audio/wav;base64," + base64.b64encode(data.getvalue()).decode()}]}]},
            "parameters": {"asr_options": {"enable_itn": True}}}, self.config["asrTimeout"])

    async def observe(self, instructions, image):
        if not image:
            raise ValueError("visual observation requires a frame")
        return await self._json("vision", self.config["visionModel"], {
            "input": {"messages": [
                {"role": "system", "content": [{"text": instructions}]},
                {"role": "user", "content": [{"image": "data:image/jpeg;base64," + image}]}]},
            "parameters": {"temperature": 0, "enable_thinking": False, "max_tokens": 800,
                           "response_format": {"type": "json_object"}}},
            number("EGOASSIST_PERCEPTION_TIMEOUT", 8))

    async def text_stream(self, messages):
        started = time.perf_counter()
        result = {"stage": "text", "model": self.config["textModel"], "apiCalls": 1, "cacheHits": 0, "cost": None,
                  "firstTokenMs": None, "inputMessages": len(messages),
                  "inputCharacters": sum(len(message_text(row.get("content"))) for row in messages)}
        terminal = False
        body = {"model": self.config["textModel"], "input": {"messages": messages}, "parameters": {
            "result_format": "message", "temperature": 0, "enable_thinking": False,
            "incremental_output": True, "max_tokens": 1200}}
        call_id = self.audit_request('text', self.config['textModel'], body)
        output_parts = []
        provider_events = []
        try:
            async with self.http().post(self.url("/services/aigc/text-generation/generation"),
                                        json=body, headers={"X-DashScope-SSE": "enable"}) as response:
                response.raise_for_status()
                async for raw in response.content:
                    line = raw.decode("utf8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    provider_events.append(event)
                    result['requestId'] = event.get('request_id', result.get('requestId'))
                    if event.get("code"):
                        raise RuntimeError("text provider error: " + str(event["code"]))
                    choices = event.get("output", {}).get("choices", [])
                    result["usage"] = event.get("usage", result.get("usage"))
                    if not choices:
                        continue
                    choice = choices[0]
                    text = message_text(choice.get("message", {}).get("content"))
                    if text:
                        output_parts.append(text)
                    finish = choice.get("finish_reason")
                    if finish not in (None, "null", "stop"):
                        raise ValueError("text response was truncated")
                    terminal = terminal or finish == "stop"
                    if text:
                        if result["firstTokenMs"] is None:
                            result["firstTokenMs"] = (time.perf_counter() - started) * 1000
                        yield text
            if not terminal or result["firstTokenMs"] is None:
                raise ValueError("text stream ended without a complete answer")
            result["status"] = "completed"
        except BaseException as exc:
            result.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=type(exc).__name__)
            raise
        finally:
            self.audit({'auditEvent': 'response', 'callId': call_id, **result,
                        'output': {'text': ''.join(output_parts), 'events': provider_events} if provider_events else None,
                        'latencyMs': (time.perf_counter() - started) * 1000,
                        'outputFormat': 'provider-json-events-and-assembled-text'})
            self.audit({**result, "latencyMs": (time.perf_counter() - started) * 1000})

    async def speak_stream(self, text_iterator, emit_audio):
        started = time.perf_counter()
        result = {"stage": "tts", "model": self.config["ttsModel"], "apiCalls": 1, "cacheHits": 0, "cost": None,
                  "firstAudioMs": None, "audioBytes": 0, "sampleRate": 24000,
                  "connectMs": None, "sessionReadyMs": None, "firstTextSentMs": None,
                  "textFinishedMs": None, "firstTextToAudioMs": None}
        producer = None
        ready = asyncio.Event()
        ws = None
        result["textPrefetch"] = self.config.get("ttsPrefetch", True)
        session_config = {"voice": self.config["voice"], "mode": "server_commit",
                          "response_format": "pcm", "sample_rate": 24000}
        call_id = self.audit_request('tts', self.config['ttsModel'], {'session': session_config})
        output_audio = bytearray()

        async def send(kind, **payload):
            await ws.send_json({"event_id": uuid4().hex, "type": kind, **payload})

        async def produce():
            # Start the admitted answer while TTS connects. At most one chunk
            # waits locally, and no text is submitted before session.updated.
            async with aclosing(text_iterator):
                async for text in text_iterator:
                    if result.get("firstTextReadyMs") is None:
                        result["firstTextReadyMs"] = (time.perf_counter() - started) * 1000
                    await ready.wait()
                    await send("input_text_buffer.append", text=text)
                    self.audit({'auditEvent': 'input', 'callId': call_id, 'stage': 'tts',
                                'model': self.config['ttsModel'], 'text': text})
                    if result["firstTextSentMs"] is None:
                        result["firstTextSentMs"] = (time.perf_counter() - started) * 1000
                await ready.wait()
                await send("session.finish")
                result["textFinishedMs"] = (time.perf_counter() - started) * 1000

        try:
            async with asyncio.timeout(self.config["timeout"]):
                if result["textPrefetch"]:
                    producer = asyncio.create_task(produce())
                async with self.http().ws_connect(
                    TTS_URL,
                    params={"model": self.config["ttsModel"]}, heartbeat=15, max_msg_size=4 * 1024 * 1024) as ws:
                    result["connectMs"] = (time.perf_counter() - started) * 1000
                    await send("session.update", session=session_config)
                    # Wait for session.updated before submitting text. Propagate a
                    # producer failure immediately instead of waiting on the socket.
                    finished = False
                    while not finished:
                        if producer and producer.done():
                            producer.result()
                        receive = asyncio.create_task(ws.receive())
                        try:
                            waiting = {receive, producer} if producer and not producer.done() else {receive}
                            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                            if producer and producer in done:
                                producer.result()
                            event = await receive
                        finally:
                            if not receive.done():
                                receive.cancel()
                                await asyncio.gather(receive, return_exceptions=True)
                        if event.type != aiohttp.WSMsgType.TEXT:
                            raise ConnectionError("TTS socket closed before session.finished")
                        payload = json.loads(event.data)
                        kind = payload.get("type")
                        if kind == "session.updated" and not ready.is_set():
                            result["sessionReadyMs"] = (time.perf_counter() - started) * 1000
                            ready.set()
                            if producer is None:
                                producer = asyncio.create_task(produce())
                        elif kind == "response.audio.delta":
                            audio = base64.b64decode(payload["delta"], validate=True)
                            if len(audio) % 2:
                                raise ValueError("invalid PCM16 TTS chunk")
                            if result["firstAudioMs"] is None:
                                result["firstAudioMs"] = (time.perf_counter() - started) * 1000
                                if result["firstTextSentMs"] is not None:
                                    result["firstTextToAudioMs"] = result["firstAudioMs"] - result["firstTextSentMs"]
                            result["audioBytes"] += len(audio)
                            output_audio.extend(audio)
                            emit_audio(payload["delta"])
                        elif kind == "error":
                            raise RuntimeError("TTS provider error: " + str(payload.get("error", {}).get("code", "unknown")))
                        elif kind == "session.finished":
                            finished = True
                        if payload.get("usage"):
                            result["usage"] = payload["usage"]
                    if producer:
                        await producer
                    if not result["audioBytes"]:
                        raise ValueError("TTS returned no audio")
                    result["status"] = "completed"
        except BaseException as exc:
            result.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=type(exc).__name__)
            raise
        finally:
            if producer:
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            wav_output = io.BytesIO()
            with wave.open(wav_output, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(output_audio)
            self.audit({'auditEvent': 'response', 'callId': call_id, **result,
                        'output': {'audio': 'data:audio/wav;base64,' + base64.b64encode(wav_output.getvalue()).decode()} if output_audio else None,
                        'latencyMs': (time.perf_counter() - started) * 1000,
                        'outputFormat': 'received-pcm-wrapped-as-wav'})
            self.audit({**result, "latencyMs": (time.perf_counter() - started) * 1000})

    async def close(self):
        if self.session:
            await self.session.close()


class ModularConversation:
    uses_http_pipeline = True

    def __init__(self, *, model, callback, api_key, url=None, provider_factory=ModularProvider, vad=None, text_only=False):
        self.loop = asyncio.get_running_loop()
        self.callback, self.api_key = callback, api_key
        self.config = settings()
        self.model = self.config["textModel"]
        self.provider = provider_factory(api_key, self.config, self._audit)
        self.session = {}
        self.items = deque()
        self.item_ids = set()
        self.image = None
        self.closed = False
        self.response = None
        self.asr_worker = None
        self.asr_queue = asyncio.Queue(maxsize=self.config["asrQueue"])
        self.tasks = set()
        self.vad = vad
        self.segmenter = None
        self.text_only = text_only
        self.active_response_id = None
        self.active_context_item_ids = []
        self.response_usage = None
        self.audit_calls = {}
        self.active_asr_id = None

    def _event(self, kind, **data):
        if not self.closed:
            self.callback.on_event({"type": kind, **data})

    def _audit(self, row):
        # Audit terminal cancelled requests as well; no credentials in events.
        if row.get('auditEvent'):
            if row['auditEvent'] == 'request':
                self.audit_calls[row['callId']] = ({'itemId': self.active_asr_id} if row.get('stage') == 'asr'
                                                  else {'responseId': self.active_response_id,
                                                        'contextItemIds': list(self.active_context_item_ids)})
            context = self.audit_calls.get(row['callId'], {})
            self.callback.on_event({'type': 'pipeline.audit', **row, **context})
            if row['auditEvent'] == 'response':
                self.audit_calls.pop(row['callId'], None)
            return
        if row.get("stage") in {"text", "vision", "tts"}:
            row = {**row, "responseId": self.active_response_id}
            if row["stage"] in {"text", "vision"} and isinstance(row.get("usage"), dict):
                self.response_usage = deepcopy(row["usage"])
        self.callback.on_event({"type": "pipeline.metrics", **row})

    def _on_loop(self, function, *args):
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self.loop:
            function(*args)
        else:
            self.loop.call_soon_threadsafe(function, *args)

    def _task(self, coroutine):
        task = self.loop.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def connect(self):
        if not self.api_key:
            raise ValueError("QWEN_API_KEY is required")
        self.callback.on_open()

    def update_session(self, **session):
        self.session.update(session)
        if session.get("enable_input_audio_transcription") and not self.text_only and self.segmenter is None:
            self.segmenter = SpeechSegments(self._speech_start, self._speech_end,
                                           max_seconds=self.config["maxAudioSeconds"], vad=self.vad)

    def _speech_start(self, sid, start):
        self._event("input_audio_buffer.speech_started", item_id=sid, audio_start_ms=start)

    def _speech_end(self, sid, end, pcm):
        self._event("input_audio_buffer.speech_stopped", item_id=sid, audio_end_ms=end)
        try:
            self.asr_queue.put_nowait((sid, pcm))
        except asyncio.QueueFull:
            self._event("conversation.item.input_audio_transcription.completed", item_id=sid, transcript="")
            self._event("pipeline.error", stage="asr", reason="queue-full", itemId=sid)
        if self.asr_worker is None or self.asr_worker.done():
            self.asr_worker = self._task(self._transcribe())

    async def _transcribe(self):
        while not self.closed and not self.asr_queue.empty():
            sid, pcm = self.asr_queue.get_nowait()
            self.active_asr_id = sid
            try:
                text = (await self.provider.transcribe(pcm)).strip()
                # The application's transcript handler owns context insertion.
                self._event("conversation.item.input_audio_transcription.completed", item_id=sid, transcript=text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event("conversation.item.input_audio_transcription.completed", item_id=sid, transcript="")
                self._event("pipeline.error", stage="asr", reason=type(exc).__name__, itemId=sid)
            finally:
                self.active_asr_id = None
                self.asr_queue.task_done()

    def append_audio(self, audio):
        if self.closed or not self.session.get("enable_input_audio_transcription"):
            return
        pcm = base64.b64decode(audio, validate=True)
        if len(pcm) > 32000:
            raise ValueError("audio chunks must not exceed one second")
        self._on_loop(self.segmenter.feed, pcm)

    def append_video(self, image):
        if len(image) > 4 * 1024 * 1024:
            raise ValueError("visual frame too large")
        # Dialogue receives question-bound visual evidence through the existing
        # context harness; only isolated observation sessions consume raw frames.
        if not self.session.get("enable_input_audio_transcription"):
            self.image = image

    def commit(self):
        pass  # Visual HTTP requests have no audio ordering requirement.

    def create_item(self, item):
        self._on_loop(self._store_item, deepcopy(item))

    def _store_item(self, item):
        key = item.get("id")
        if self.closed or (key and key in self.item_ids):
            return
        if item.get("type") == "function_call_output":
            value = {"role": "user", "content": json.dumps({"toolReceipt": item.get("output"), "callId": item.get("call_id")})}
        elif item.get("type") == "message" and item.get("role") in {"user", "assistant", "system"}:
            value = {"role": item["role"], "content": message_text(item.get("content"))}
        else:
            raise ValueError("unsupported replay item")
        if len(value["content"]) > self.config["historyChars"]:
            raise ValueError("context item exceeds the modular history budget")
        self.items.append((key, value))
        if key:
            self.item_ids.add(key)
        while len(self.items) > 128 or sum(len(row[1]["content"]) for row in self.items) > self.config["historyChars"]:
            old, _ = self.items.popleft()
            self.item_ids.discard(old)
            self._audit({"stage": "context", "status": "evicted-oldest-item", "apiCalls": 0, "cost": None})

    def create_response(self, *, instructions, output_modalities, prepared_answer=None, context_items=None):
        self._on_loop(self._start_response, instructions, list(output_modalities), prepared_answer,
                      deepcopy(context_items))

    def _start_response(self, instructions, modalities, prepared_answer=None, context_items=None):
        if self.closed:
            return
        if self.response and not self.response.done():
            self._event("error", error={"message": "conversation already has an active response"})
            return
        if context_items is not None:
            # The journal owns accepted user turns and committed answers. The
            # live adapter alone retains only its own outputs, and queued turns
            # must not read input that arrived after their request was captured.
            self.items.clear()
            self.item_ids.clear()
            for item in context_items:
                self._store_item(item)
        sid = "modular-" + uuid4().hex
        self.active_response_id = sid
        self.active_context_item_ids = [item_id for item_id, _ in self.items]
        self.response_usage = None
        response_model = prepared_answer["model"] if prepared_answer else self.model
        # Freeze before yielding or publishing response.created. New frames,
        # input items and session updates must belong to the next response.
        context = {'visual': not prepared_answer and not self.session.get('enable_input_audio_transcription'),
                   'image': self.image, 'instructions': self.session.get('instructions', ''),
                   'messages': [deepcopy(value) for _, value in self.items]}
        self.response = self._task(self._respond(sid, instructions, modalities, context, deepcopy(prepared_answer)))
        def before_start_cancelled(task):
            if task.cancelled():
                if self.response is task:
                    self.response = None
                self._event("response.cancelled", response={"id": sid, "model": response_model, "status": "cancelled"})
        self.response.add_done_callback(before_start_cancelled)
        self._event("response.created", response={"id": sid, "model": response_model})

    async def _respond(self, sid, instructions, modalities, context, prepared_answer=None):
        started = time.perf_counter()
        status = "completed"
        audio_available = None
        text_parts = []
        visual = context['visual']
        try:
            # Allow an immediate cancellation from response.created to propagate.
            await asyncio.sleep(0)
            if visual:
                text = await self.provider.observe(instructions, context['image'])
                self._event("response.text.done", response_id=sid, text=text)
            else:
                messages = [{"role": "system", "content": context['instructions']},
                            {"role": "system", "content": instructions}]
                messages += context['messages']
                audio = Modalities.AUDIO in modalities
                async def tokens():
                    if prepared_answer:
                        token = prepared_answer['text']
                        text_parts.append(token)
                        self._event("response.audio_transcript.delta" if audio else "response.text.delta", response_id=sid, delta=token)
                        self._audit({'stage': 'prepared-answer', 'model': prepared_answer['model'],
                                     'responseId': sid, 'apiCalls': 0, 'deliveryPath': 'direct-visual'})
                        yield token
                        return
                    async with aclosing(self.provider.text_stream(messages)) as stream:
                        async for token in stream:
                            text_parts.append(token)
                            self._event("response.audio_transcript.delta" if audio else "response.text.delta", response_id=sid, delta=token)
                            yield token
                if audio and self.text_only:
                    audio_available = await self._speak_text_turn(sid, tokens(), text_parts)
                elif audio:
                    await self.provider.speak_stream(tokens(), lambda chunk: self._event("response.audio.delta", response_id=sid, delta=chunk))
                else:
                    async for _ in tokens():
                        pass
                text = "".join(text_parts)
                if not text.strip():
                    raise ValueError("empty answer")
                self._event("response.audio_transcript.done" if audio else "response.text.done", response_id=sid,
                            **({"transcript": text} if audio else {"text": text}))
                self._store_item({"id": sid, "type": "message", "role": "assistant", "content": text})
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:
            status = "failed"
            self._event("pipeline.error", stage="vision" if visual else "answer", reason=type(exc).__name__, responseId=sid)
        finally:
            # Release before the terminal callback dispatches the next response.
            if self.response is asyncio.current_task():
                self.response = None
            self._audit({"stage": "response", "status": status, "responseId": sid,
                         "latencyMs": (time.perf_counter() - started) * 1000, "apiCalls": 0, "cost": None})
            self._event("response.done" if status == "completed" else "response." + status,
                        response={"id": sid, "model": prepared_answer["model"] if prepared_answer else self.config["visionModel"] if visual else self.model,
                                  "status": status, "usage": self.response_usage, "audioAvailable": audio_available})

    async def _speak_text_turn(self, sid, tokens, text_parts):
        changed = asyncio.Event()

        async def generate():
            try:
                async with aclosing(tokens):
                    async for _ in tokens:
                        changed.set()
            finally:
                changed.set()

        producer = self._task(generate())

        async def speech_tokens():
            index = 0
            while True:
                while index < len(text_parts):
                    token = text_parts[index]
                    index += 1
                    yield token
                if producer.done():
                    await producer
                    return
                changed.clear()
                await changed.wait()

        try:
            async with aclosing(speech_tokens()) as stream:
                await self.provider.speak_stream(stream, lambda chunk: self._event("response.audio.delta", response_id=sid, delta=chunk))
            return True
        except Exception as exc:
            # Text generation is independent of TTS connection and playback.
            # A failed text producer still propagates as an answer failure.
            await producer
            self._event("pipeline.warning", stage="tts", reason=type(exc).__name__, responseId=sid)
            return False
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    def cancel_response(self):
        def cancel():
            if self.response and not self.response.done():
                self.response.cancel()
        self._on_loop(cancel)

    async def aclose(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.provider.close()
        self.items.clear()
        self.item_ids.clear()
        self.image = None
        self.segmenter = None
        while not self.asr_queue.empty():
            self.asr_queue.get_nowait()
            self.asr_queue.task_done()

    def close(self):
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self.loop:
            return self.loop.create_task(self.aclose())
        asyncio.run_coroutine_threadsafe(self.aclose(), self.loop).result(timeout=10)
