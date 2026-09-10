"""Shared scene evidence independent of skills, with immutable question-time media."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import time
import wave

import aiohttp
from PIL import Image

from egoassist.prompts import prompt_text
from .speakers import MODELS, SpeakerRegistry
from .visual_question import VisualQuestions
from .settings import asr_settings


def restrict_unverified_speaker(semantic):
    """Retain explicit commands; voice identity is evidence, not authorization."""
    value = dict(semantic)
    control = value.get("interaction") or {}
    if not control:
        value["interaction"] = {"audience": "uncertain", "action": "uncertain", "output": "silent"}
    value["speakerAttribution"] = "unconfirmed"
    return value


class SharedScene:
    def __init__(self, journal, api_key):
        self.journal, self.api_key = journal, api_key
        self.root = Path(journal.path).parent / "media" / journal.session_id
        self.frames = deque(maxlen=64)
        self.audio = bytearray()
        self.audio_samples = 0
        self.audio_epoch = None
        self.audio_clock = deque(maxlen=2400)
        self.speakers = SpeakerRegistry(journal)
        self.turns = {r["turnId"]: r for r in journal.entries() if r.get("type") == "scene.turn"}
        self.last_vision = None
        self.vision_task = None
        self.vision_by_frame = {}
        self.tasks = set()
        self.vision_lock = asyncio.Semaphore(2)
        self.vision_pending = {}
        self.vision_progress = {}
        self.enriching = set()
        self.restored = False
        self.visual_questions = VisualQuestions(self)
        self._http = None
        self.asr = asr_settings()
        self.transcription_limit = asyncio.Semaphore(self.asr["concurrency"])
        for row in journal.entries():
            if row.get("type") == "scene.result":
                self.last_vision = (0, row)
                if row.get("status") == "observed":
                    self.vision_by_frame[row["frame"]["frameId"]] = row
        while len(self.vision_by_frame) > 16:
            self.vision_by_frame.pop(next(iter(self.vision_by_frame)))

    def _save(self, data, suffix, mime):
        digest = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (digest + suffix)
        if not path.exists():
            path.write_bytes(data)
        return {"assetId": path.name, "sha256": digest, "mime": mime, "bytes": len(data)}

    def http(self):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(trust_env=True, connector=aiohttp.TCPConnector(limit=6))
        return self._http

    def media(self, ref):
        name = ref["assetId"]
        if Path(name).name != name or not name.startswith(ref["sha256"]):
            raise ValueError("invalid media reference")
        data = (self.root / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError("media evidence checksum mismatch")
        return data

    def append_frame(self, image, metadata=None, *, describe=True):
        data = base64.b64decode(image, validate=True)
        if len(data) > 2_000_000:
            raise ValueError("scene frame too large")
        with Image.open(io.BytesIO(data)) as frame:
            if frame.format != "JPEG" or frame.width * frame.height > 4_000_000:
                raise ValueError("scene expects a bounded JPEG frame")
            frame.verify()
        now = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(data).hexdigest()
        self.frames.append({"frameId": digest[:24], "receivedAt": now, "monotonic": time.monotonic(),
                            "device": metadata or {}, "data": data})
        interval = float(os.getenv("EGOASSIST_SCENE_INTERVAL", "4"))
        if describe and self.api_key and (not self.vision_task or self.vision_task.done()) and (
                not self.last_vision or time.monotonic() - self.last_vision[0] >= interval):
            ref = self._frame_ref(self.frames[-1])
            self.vision_task = asyncio.create_task(self.describe(ref), name="shared-scene-vision")
            self.tasks.add(self.vision_task)
            self.vision_task.add_done_callback(self.tasks.discard)

    def _frame_ref(self, frame):
        return {**self._save(frame["data"], ".jpg", "image/jpeg"),
                "frameId": frame["frameId"], "receivedAt": frame["receivedAt"], "device": frame["device"]}

    def append_audio(self, audio, epoch, *, sample_offset=0):
        data = base64.b64decode(audio, validate=True)
        if len(data) % 2 or len(data) > 320_000:
            raise ValueError("invalid PCM16 input")
        if epoch != self.audio_epoch:
            self.audio.clear()
            self.audio_samples = sample_offset
            self.audio_epoch = epoch
            self.audio_clock.clear()
        self.audio.extend(data)
        self.audio_samples += len(data) // 2
        self.audio_clock.append((self.audio_samples, time.monotonic()))
        limit = 16000 * 2 * 90
        if len(self.audio) > limit:
            del self.audio[:-limit]

    def capture(self, turn_id, *, segment=None, explicit=False):
        if turn_id in self.turns:
            return self.turns[turn_id]
        frame = self.frames[-1] if self.frames and time.monotonic() - self.frames[-1]["monotonic"] <= 5 else None
        result = {"turnId": turn_id, "frame": self._frame_ref(frame) if frame and (explicit or not segment) else None,
                  "speaker": {"status": "account-input" if explicit else "pending", "wearer": explicit},
                  "segment": segment, "audio": None, "synchronizedFrames": [],
                  "avAssociation": {"status": "unlinked", "reason": "Time coincidence alone does not identify the speaker"}}
        if segment and segment.get("audioEndMs") is not None and segment.get("audioStartMs") is not None:
            start, end = int(segment["audioStartMs"] * 16), int(segment["audioEndMs"] * 16)
            first = self.audio_samples - len(self.audio) // 2
            epoch_matches = segment.get("generation") in {None, self.audio_epoch}
            if epoch_matches and first <= start < end <= self.audio_samples and end - start <= 60 * 16000:
                pcm = bytes(self.audio[(start - first) * 2:(end - first) * 2])
                output = io.BytesIO()
                with wave.open(output, "wb") as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(pcm)
                result["audio"] = self._save(output.getvalue(), ".wav", "audio/wav")
                # Associate receipt times using each actual audio chunk, not ASR completion time.
                audio_start = next((received - max(0, sample - start) / 16000 for sample, received in self.audio_clock if sample >= start), None)
                audio_end = next((received - max(0, sample - end) / 16000 for sample, received in self.audio_clock if sample >= end), None)
                if audio_start is not None and audio_end is not None:
                    candidates = [f for f in self.frames if audio_start - 0.5 <= f["monotonic"] <= audio_end + 0.5]
                    stride = max(1, len(candidates) // 8)
                    result["synchronizedFrames"] = [{**self._frame_ref(f), "offsetMs": round((f["monotonic"] - audio_start) * 1000),
                        "syncBasis": "server-receipt", "matchingWindowMs": 500, "syncAccuracy": "unmeasured"} for f in candidates[::stride][:8]]
                    if result["synchronizedFrames"]:
                        result["frame"] = result["synchronizedFrames"][-1]
            else:
                result["speaker"] = {"status": "unknown", "wearer": False, "reason": "audio-range-unavailable"}
        self.turns[turn_id] = self.journal.append("scene.turn", result)
        return self.turns[turn_id]

    async def resolve_speaker(self, turn_id):
        row = self.turns.get(turn_id)
        if not row or row["speaker"]["status"] != "pending":
            return row
        try:
            if not row.get("audio"):
                speaker = {"status": "unknown", "wearer": False, "reason": "no-audio-evidence"}
            else:
                with wave.open(io.BytesIO(self.media(row["audio"]))) as wav:
                    pcm = wav.readframes(wav.getnframes())
                analysis = await asyncio.to_thread(MODELS.analyze, pcm)
                speaker = self.speakers.match(analysis)
        except Exception as exc:
            speaker = {"status": "unavailable", "wearer": False, "reason": type(exc).__name__}
        self.turns[turn_id] = self.journal.append("scene.turn", {
            **{k: v for k, v in row.items() if k not in {"id", "type", "timestamp", "parentId"}}, "speaker": speaker})
        if len({s.get("cluster") for s in speaker.get("segments", [])}) > 1:
            self.start_transcription(turn_id)
        return self.turns[turn_id]

    def start_transcription(self, turn_id):
        if turn_id in self.enriching or not self.api_key:
            return
        self.enriching.add(turn_id)
        task = asyncio.create_task(self.transcribe_segments(turn_id), name="scene-speaker-transcription")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda _: self.enriching.discard(turn_id))

    def recover(self):
        if self.restored:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.restored = True
        if MODELS.available() and MODELS.diarizer is None:
            task = asyncio.create_task(self.warm_speakers(), name="scene-speaker-warmup")
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        for row in self.turns.values():
            if row.get("audio") and not row.get("utterances") and len({s.get("cluster") for s in row["speaker"].get("segments", [])}) > 1:
                self.start_transcription(row["turnId"])

    async def warm_speakers(self):
        try:
            result = await asyncio.to_thread(MODELS.warmup)
        except Exception as exc:
            result = {"status": "unavailable", "reason": type(exc).__name__}
        self.journal.append("speaker.warmup", result)

    async def transcribe_segments(self, turn_id):
        row = self.turns[turn_id]
        with wave.open(io.BytesIO(self.media(row["audio"]))) as wav:
            pcm = wav.readframes(wav.getnframes())
        segments = row["speaker"].get("segments", [])

        async def transcribe(segment):
            if any(other["cluster"] != segment["cluster"] and max(other["startMs"], segment["startMs"]) < min(other["endMs"], segment["endMs"])
                   for other in segments):
                return {**segment, "text": None, "status": "overlapping-speech", "reason": "Mono overlap cannot be assigned word by word"}
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(pcm[segment["startMs"] * 32:segment["endMs"] * 32])
            ref = self._save(output.getvalue(), ".wav", "audio/wav")
            request = {"turnId": turn_id, "audio": ref, "segment": segment, **self.asr, "prompt": prompt_text("scene/asr")}
            self.journal.append("scene.transcription.request", request)
            started = time.perf_counter()
            try:
                import dashscope
                async with asyncio.timeout(self.asr["timeoutSeconds"]), self.transcription_limit:
                    async with self.http().post(dashscope.base_http_api_url.rstrip("/") + "/services/aigc/multimodal-generation/generation",
                        timeout=aiohttp.ClientTimeout(total=self.asr["timeoutSeconds"]),
                        headers={"Authorization": "Bearer " + self.api_key}, json={"model": request["model"],
                        "input": {"messages": [{"role": "system", "content": [{"text": request["prompt"]}]},
                                                {"role": "user", "content": [{"audio": "data:audio/wav;base64," + base64.b64encode(output.getvalue()).decode()}]}]},
                        "parameters": {"asr_options": {"enable_itn": True}}}) as response:
                        response.raise_for_status()
                        payload = await response.json()
                content = payload["output"]["choices"][0]["message"]["content"]
                text = content if isinstance(content, str) else "".join(c.get("text", "") for c in content)
                result = {**segment, "audio": ref, "status": "transcribed", "text": text,
                          "requestId": payload.get("request_id"), "usage": payload.get("usage")}
            except Exception as exc:
                result = {**segment, "audio": ref, "status": "unavailable", "text": None, "error": type(exc).__name__}
            result["latencyMs"] = round((time.perf_counter() - started) * 1000, 1)
            self.journal.append("scene.transcription.result", {"turnId": turn_id, **result})
            return result

        utterances = await asyncio.gather(*(transcribe(s) for s in segments))
        row = self.turns[turn_id]
        self.turns[turn_id] = self.journal.append("scene.turn", {
            **{k: v for k, v in row.items() if k not in {"id", "type", "timestamp", "parentId"}}, "utterances": utterances})

    async def describe(self, ref):
        if ref["frameId"] in self.vision_by_frame:
            return self.vision_by_frame[ref["frameId"]]
        task = self.vision_pending.get(ref["frameId"])
        if task is None:
            task = asyncio.create_task(self._describe(ref), name="scene-vision-request")
            self.vision_pending[ref["frameId"]] = task
            self.vision_progress[ref["frameId"]] = {"frameId": ref["frameId"], "status": "queued", "started": time.monotonic()}
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            task.add_done_callback(lambda _: self.vision_pending.pop(ref["frameId"], None))
            task.add_done_callback(lambda _: self.vision_progress.pop(ref["frameId"], None))
        return await asyncio.shield(task)

    async def _describe(self, ref):
        async with self.vision_lock:
            self.vision_progress[ref["frameId"]]["status"] = "waiting-provider"
            if ref["frameId"] in self.vision_by_frame:
                return self.vision_by_frame[ref["frameId"]]
            started = time.perf_counter()
            model = os.getenv("EGOASSIST_SCENE_MODEL", "qwen3-vl-flash")
            request = {"frame": ref, "model": model, "prompt": prompt_text("scene/observe")}
            self.journal.append("scene.request", request)
            try:
                import dashscope
                messages = [{"role": "system", "content": [{"text": request["prompt"]}]},
                            {"role": "user", "content": [{"image": "data:image/jpeg;base64," + base64.b64encode(self.media(ref)).decode()},
                                                            {"text": "Describe this frame. Frame ID: " + ref["frameId"]}]}]
                async with asyncio.timeout(8):
                    async with self.http().post(dashscope.base_http_api_url.rstrip("/") + "/services/aigc/multimodal-generation/generation",
                        timeout=aiohttp.ClientTimeout(total=8),
                        headers={"Authorization": "Bearer " + self.api_key}, json={"model": model, "input": {"messages": messages},
                        "parameters": {"temperature": 0, "enable_thinking": False, "max_tokens": 600, "response_format": {"type": "json_object"}}}) as response:
                        response.raise_for_status()
                        payload = await response.json()
                content = payload["output"]["choices"][0]["message"]["content"]
                raw = content if isinstance(content, str) else "".join(c.get("text", "") for c in content)
                value = json.loads(raw)
                if not isinstance(value.get("description"), str) or not isinstance(value.get("people", []), list):
                    raise ValueError("invalid scene result")
                result = {"status": "observed", "frame": ref, "description": value["description"],
                          "people": [{"personRef": ref["frameId"] + ":" + str(i), "description": str(p.get("description", "")),
                                      "position": str(p.get("position", "")), "identity": "unknown"}
                                     for i, p in enumerate(value.get("people", [])[:8]) if isinstance(p, dict)],
                          "requestId": payload.get("request_id"), "usage": payload.get("usage"), "rawOutput": raw}
            except Exception as exc:
                result = {"status": "unavailable", "frame": ref, "error": type(exc).__name__ + ": " + str(exc)[:150]}
            result.update(model=model, latencyMs=round((time.perf_counter() - started) * 1000, 1))
            self.journal.append("scene.result", result)
            self.last_vision = (time.monotonic(), result)
            if result["status"] == "observed":
                self.vision_by_frame[ref["frameId"]] = result
                while len(self.vision_by_frame) > 16:
                    self.vision_by_frame.pop(next(iter(self.vision_by_frame)))
            return result

    async def context(self, turn_id, *, describe_frame=False, question=None):
        row = self.turns.get(turn_id)
        if not row:
            return None
        if not row.get("utterances") and len({s.get("cluster") for s in row["speaker"].get("segments", [])}) > 1:
            self.start_transcription(turn_id)
        # Audio and background answers already receive frames; text realtime needs a description.
        ref = row.get("frame")
        visual = self.vision_by_frame.get(ref["frameId"], {"status": "frame-available", "frame": ref}) if ref else {"status": "no-frame"}
        if ref and describe_frame and self.api_key:
            if question and os.getenv("EGOASSIST_VISUAL_QUESTION_MODE", "focused") == "focused":
                visual = await self.visual_questions.observe(turn_id, question, ref)
            else:
                visual = await self.describe(ref)
        if ref:
            visual = {**visual, "descriptionFrameReceivedAt": (visual.get("frame") or {}).get("receivedAt"), "frame": ref}
        row = self.turns.get(turn_id, row)
        association = self.association(row)
        return {"turnId": turn_id, "speaker": row["speaker"], "audio": row.get("audio"),
                "utterances": row.get("utterances", []),
                "synchronizedFrames": row.get("synchronizedFrames", []), "avAssociation": association,
                "visual": visual, "timeBasis": "question-time; receivedAt is server receipt, device timestamp is unverified"}

    def association(self, row):
        profile = self.speakers.profiles.get(row.get("speaker", {}).get("speakerId"), {})
        person = profile.get("personRef")
        frame_ids = {f["frameId"] for f in row.get("synchronizedFrames", [])}
        if row.get("frame"):
            frame_ids.add(row["frame"]["frameId"])
        if person and person.split(":", 1)[0] in frame_ids:
            return {"status": "account-confirmed", "personRef": person, "name": profile.get("name"),
                    "scope": "reference-frame-only; not live face tracking"}
        return row.get("avAssociation")

    def snapshot(self):
        return {"camera": "receiving" if self.frames and time.monotonic() - self.frames[-1]["monotonic"] < 5 else "no-fresh-frame",
                "vision": self.last_vision[1] if self.last_vision else None,
                "questionVisionPending": [{"turnId": p["turnId"], "frameId": p["frameId"], "status": p["status"],
                    "elapsedMs": round((time.monotonic() - p["started"]) * 1000),
                    "model": os.getenv("EGOASSIST_VISUAL_QUESTION_MODEL", "qwen3-vl-flash")}
                    for p in self.visual_questions.progress.values()],
                "visionPending": [{"frameId": p["frameId"], "status": p["status"],
                                   "elapsedMs": round((time.monotonic() - p["started"]) * 1000),
                                   "model": os.getenv("EGOASSIST_SCENE_MODEL", "qwen3-vl-flash"), "requestTimeoutMs": 8000}
                                  for p in self.vision_progress.values()],
                "asr": dict(self.asr),
                "speakerModel": "available" if MODELS.available() else "unavailable",
                "speakers": self.speakers.public(), "utterances": [{**row, "avAssociation": self.association(row)} for row in self.turns.values()],
                "association": "Names and voice-to-person links require account confirmation; unlinked faces remain unknown"}

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._http is not None:
            await self._http.close()
            self._http = None
        self.restored = False
