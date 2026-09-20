"""Real-provider bridge comparison; synthetic audio is not hardware validation."""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import time
import wave

import numpy as np
from PIL import Image, ImageDraw

from egoassist._env import load_env
from egoassist.realtime import ContextHarness, ContextSettings, SessionJournal
from egoassist.realtime.modular import ModularProvider, settings
from egoassist.realtime.omni import OmniBrowserBridge


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")


async def until(predicate, timeout=60):
    async with asyncio.timeout(timeout):
        while not predicate(): await asyncio.sleep(.02)


def wav_file(path, pcm, rate):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


async def compare(args):
    load_env(".env")
    key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not key: raise ValueError("QWEN_API_KEY is required")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"mock": False, "runs": [], "cost": None, "humanAnswerQuality": None,
              "physicalMicrophoneTested": False, "physicalEchoCancellationTested": False,
              "claimBoundary": "Synthetic TTS input, generated visual fixture, real ASR/text/VL/TTS/Omni requests. No human quality or physical-device latency claim.",
              "fixture": "Please remember that the blue cup is on the desk.", "fixtureMetrics": []}
    save(args.output / "plan.json", {**report, "backends": args.backends, "repetitions": args.repetitions,
                                    "modular": settings(), "omniModel": args.model})
    provider = ModularProvider(key, settings(), report["fixtureMetrics"].append)
    pcm_chunks = []
    async def phrase(): yield report["fixture"]
    try:
        await provider.speak_stream(phrase(), lambda value: pcm_chunks.append(base64.b64decode(value)))
    finally:
        await provider.close()
        save(args.output / "comparison.json", report)
    pcm24 = b"".join(pcm_chunks)
    wav_file(args.output / "fixture-24k.wav", pcm24, 24000)
    original = np.frombuffer(pcm24, dtype="<i2")
    pcm = np.interp(np.arange(0, len(original), 1.5), np.arange(len(original)), original).astype("<i2").tobytes()
    pcm += bytes(32000)
    wav_file(args.output / "fixture-16k.wav", pcm, 16000)
    report["fixturePcmSha256"] = hashlib.sha256(pcm).hexdigest()
    # Simple synthetic visual truth, identical for both providers.
    picture = Image.new("RGB", (640, 480), "white")
    ImageDraw.Draw(picture).rectangle((170, 90, 470, 390), fill="red")
    picture.save(args.output / "visual-fixture.png")
    encoded = io.BytesIO()
    picture.save(encoded, format="JPEG")
    frame = base64.b64encode(encoded.getvalue()).decode()
    original_backend = os.environ.get("EGOASSIST_REALTIME_BACKEND")
    try:
        for repeat in range(args.repetitions):
            order = args.backends if repeat % 2 == 0 else list(reversed(args.backends))
            for backend in order:
                os.environ["EGOASSIST_REALTIME_BACKEND"] = backend
                name = f"{backend}-{repeat}"
                row = {"backend": backend, "repeat": repeat, "events": [], "traces": [], "passed": False,
                       "cost": None, "firstAudioMs": None, "cancelMs": None}
                report["runs"].append(row)
                events, transcripts, traces = row["events"], [], row["traces"]
                audio = []
                started = time.perf_counter()
                async def emit(event):
                    if event.get("type") == "audio": audio.append(base64.b64decode(event["audio"]))
                    events.append({**{k: v for k, v in event.items() if k != "audio"},
                                   "audioBytes": len(base64.b64decode(event["audio"])) if "audio" in event else None,
                                   "atMs": (time.perf_counter() - started) * 1000})
                def trace(kind, source, payload, parent_ids=None, **kwargs):
                    entry = {"eventType": kind, "payload": payload, **kwargs, "atMs": (time.perf_counter() - started) * 1000}
                    traces.append(entry)
                    return entry
                bridge = OmniBrowserBridge(key, args.model, "Ethan",
                    "Answer briefly from the provided evidence. Do not invent facts. The blue cup is on the desk.", emit, transcripts.append)
                harness = ContextHarness(SessionJournal(args.output / f"{name}.jsonl", session_id=name), ContextSettings())
                bridge.set_context_harness(harness)
                bridge.set_trace_sink(trace)
                try:
                    await bridge.connect()
                    assert bridge.connected
                    row["connectionMs"] = (time.perf_counter() - started) * 1000
                    question = "Where is the blue cup? Answer in one short English sentence."
                    harness.record_user(question, source="comparison-text")
                    bridge.conversation.create_item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": question}]})
                    requested = time.perf_counter()
                    bridge.request_response(question, origin={"turnId": name})
                    await until(lambda: any(e["type"] == "response_status" and e["status"] in {"completed", "interrupted"} for e in events))
                    terminal = next(e for e in events if e["type"] == "response_status" and e["status"] in {"completed", "interrupted"})
                    assert terminal["status"] == "completed", terminal
                    first = next(e for e in events if e["type"] == "audio")
                    row["firstAudioMs"] = first["atMs"] - (requested - started) * 1000
                    row["answerMs"] = (time.perf_counter() - requested) * 1000
                    row["answer"] = next((e["text"] for e in reversed(events) if e["type"] == "assistant_transcript" and e.get("done")), "")
                    assert "desk" in row["answer"].lower(), row["answer"]
                    assert audio
                    wav_file(args.output / f"{name}-answer.wav", b"".join(audio), 24000)
                    visuals = []
                    lane = bridge.perception
                    lane.on_decision = lambda *value: visuals.append(value)
                    lane._GATE_INSTRUCTIONS = 'Inspect the image. Output ONLY one valid JSON object, no markdown, no code fences, no commentary. Required keys: speak (false), utterance (empty string), reason ("none"), scene_summary (visible color and shape).'
                    lane.request(frame)
                    await until(lambda: visuals)
                    row["visual"] = visuals[0]
                    # Parser defaults alone cannot satisfy the visual assertion.
                    row["visualPassed"] = "red" in json.dumps(visuals[0]).lower()
                    audio_sent = time.perf_counter()
                    for offset in range(0, len(pcm), 3200):
                        bridge.append_audio(base64.b64encode(pcm[offset:offset + 3200]).decode())
                        await asyncio.sleep(.1)
                    await until(lambda: transcripts)
                    row["recognizedText"] = transcripts[-1]
                    row["audioStartToTranscriptMs"] = (time.perf_counter() - audio_sent) * 1000
                    assert "cup" in transcripts[-1].lower() and "desk" in transcripts[-1].lower()
                    assert not bridge.has_unresolved_user_input()
                    # Interrupt a second real response, then prove the scheduler
                    # can complete a fresh response on the same connection.
                    before = len(events)
                    bridge.request_response("Explain the history of arithmetic in at least twenty sentences.")
                    await until(lambda: any(e["type"] == "audio" for e in events[before:]))
                    cancel_start = time.perf_counter()
                    bridge.cancel_response()
                    await until(lambda: any(e["type"] == "response_status" and e["status"] == "interrupted" for e in events[before:]))
                    row["cancelMs"] = (time.perf_counter() - cancel_start) * 1000
                    terminal_index = next(i for i in range(before, len(events)) if events[i]["type"] == "response_status" and events[i]["status"] == "interrupted")
                    cancelled_id = events[terminal_index]["responseId"]
                    await asyncio.sleep(.3)
                    assert not any(e["type"] == "audio" and e.get("responseId") == cancelled_id for e in events[terminal_index + 1:])
                    before = len(events)
                    bridge.request_response("Say only: Ready.")
                    await until(lambda: any(e["type"] == "response_status" and e["status"] in {"completed", "interrupted"} for e in events[before:]))
                    assert next(e for e in events[before:] if e["type"] == "response_status" and e["status"] in {"completed", "interrupted"})["status"] == "completed"
                    row["passed"] = row["visualPassed"]
                except Exception as exc:
                    row["error"] = type(exc).__name__ + ": " + str(exc)[:1000]
                finally:
                    await bridge.close()
                    row["pipelineMetrics"] = [t["payload"] for t in traces if t["eventType"] == "provider.pipeline.metrics"]
                    save(args.output / "comparison.json", report)
                    print(json.dumps({k: v for k, v in row.items() if k not in {"events", "traces", "pipelineMetrics"}}, ensure_ascii=True), flush=True)
    finally:
        if original_backend is None: os.environ.pop("EGOASSIST_REALTIME_BACKEND", None)
        else: os.environ["EGOASSIST_REALTIME_BACKEND"] = original_backend
    report["passed"] = bool(report["runs"]) and all(r["passed"] for r in report["runs"])
    report["summary"] = {backend: {"runs": sum(r["backend"] == backend for r in report["runs"]),
        "firstAudioMedianMs": statistics.median([r["firstAudioMs"] for r in report["runs"] if r["backend"] == backend and r["firstAudioMs"] is not None])
        if any(r["backend"] == backend and r["firstAudioMs"] is not None for r in report["runs"]) else None}
        for backend in args.backends}
    save(args.output / "comparison.json", report)
    return report["passed"]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--backends", nargs="+", choices=("omni", "modular"), default=["modular", "omni"])
    p.add_argument("--repetitions", type=int, default=2)
    p.add_argument("--model", default="qwen3.5-omni-flash-realtime")
    raise SystemExit(0 if asyncio.run(compare(p.parse_args())) else 1)
