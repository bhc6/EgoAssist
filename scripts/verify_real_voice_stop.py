"""Real Qwen TTS -> PCM -> realtime ASR -> semantic silence/cancellation.

Synthetic speech exercises the actual audio transport and providers, not a
physical microphone, echo cancellation, or glasses speaker latency.
"""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
import wave

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.realtime import SemanticTurnRouter
from run_egoassist import create_app
from real_session_support import journal
from verify_real_skill_lifecycle import connect
from research_interruption_semantics import assess


def decode_fixture(wav):
    with wave.open(io.BytesIO(wav)) as stream:
        assert stream.getsampwidth() == 2
        rate, channels = stream.getframerate(), stream.getnchannels()
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").reshape(-1, channels).mean(axis=1)
    pcm = np.interp(np.arange(round(len(samples) * 16000 / rate)) * rate / 16000,
                    np.arange(len(samples)), samples).astype("<i2").tobytes()
    return pcm, {"inputRate": rate, "durationMs": len(pcm) / 32,
                 "pcmSha256": hashlib.sha256(pcm).hexdigest()}


async def synthesize(text, output, index, voice="Cherry"):
    import dashscope
    api_key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=40)) as http:
        async with http.post(dashscope.base_http_api_url.rstrip("/") + "/services/aigc/multimodal-generation/generation",
            headers={"Authorization": "Bearer " + api_key}, json={"model": "qwen3-tts-flash",
                "input": {"text": text, "voice": voice, "language_type": "Chinese"}}) as response:
            payload = await response.json()
            assert response.status == 200, {"status": response.status, "code": payload.get("code")}
        audio = payload["output"]["audio"]
        async with http.get(audio["url"]) as response:
            response.raise_for_status()
            wav = await response.read()
    (output / f"input-{index}.wav").write_bytes(wav)
    # The fixture resampler only adapts TTS output to the real 16 kHz PCM input.
    pcm, metadata = decode_fixture(wav)
    return pcm, {"requestId": payload.get("request_id"), "usage": payload.get("usage"), **metadata}


async def verify(output, text_only=False, fixture_dir=None):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "mock": False, "hardwareMeasured": False, "semantic": [], "audio": [],
              "backend": os.getenv("EGOASSIST_REALTIME_BACKEND", "omni"),
              "fixtureDirectory": str(fixture_dir) if fixture_dir else None}
    router = SemanticTurnRouter(os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))
    try:
        for phrase, stop, cancel in [
            ("闭嘴。", True, False),
            ("让我安静一会儿，先别出声。", True, False),
            ("这个答案我不需要了，不用继续回答。", True, True),
            ("他说了一句闭嘴，这样说礼貌吗？", False, False),
            ("别停，请接着解释。", False, False),
            ("停止之前的回答，换个问题：九乘九是多少？", False, True),
        ]:
            router.set_turn_context(clock_context=None, memory_draft=None,
                previous_turn={"turnId": "pause", "text": "先别出声"},
                previous_answer={"turnId": "question", "text": "解释乘法原理", "status": "queued"})
            result = await router.classify(phrase, [])
            row = {"text": phrase, "expectedStop": stop, "expectedCancel": cancel, "decision": result}
            report["semantic"].append(row)
            assert result["status"] == "classified", row
            _, failures = assess({"stop": stop, "cancel": cancel}, result)
            assert not failures, row
            if cancel:
                target = (result.get("interaction") or {}).get("targetTurnId") or (result.get("revision") or {}).get("supersedesTurnId")
                assert target == "question", row
            row["passed"] = True
        if not text_only:
            phrases = [("闭嘴。", False), ("让我安静一会儿，先别出声。", False),
                       ("这个答案我不需要了，不用继续回答。", True)]
            if fixture_dir:
                fixtures = [decode_fixture((fixture_dir / f"input-{index}.wav").read_bytes()) for index in range(len(phrases))]
            else:
                fixtures = [await synthesize(text, output, index) for index, (text, _) in enumerate(phrases)]
            app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                             "qwen3.5-omni-flash-realtime", "Ethan")
            async with TestClient(TestServer(app)) as client:
                assert (await client.post("/api/auth/register", json={"username": "voice-stop",
                    "password": "isolated-voice-stop-123"})).status == 200
                for index, ((phrase, cancel), (pcm, fixture)) in enumerate(zip(phrases, fixtures)):
                    sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
                    ws, _ = await connect(client, sid)
                    tid = uuid4().hex
                    await ws.send_json({"type": "control", "clientRequestId": tid,
                        "transcript": "请详细解释乘法原理，至少讲二十句话。"})
                    async with asyncio.timeout(45):
                        while True:
                            event = await ws.receive_json()
                            assert event.get("type") not in {"error", "provider_unavailable"}, event
                            if event.get("type") == "audio" and event.get("origin", {}).get("turnId") == tid:
                                break
                    row = {"text": phrase, "sessionId": sid, "originalTurnId": tid, "fixture": fixture,
                           "events": [], "passed": False}
                    report["audio"].append(row)
                    started = time.perf_counter()

                    async def stream_pcm():
                        # Actual wall-clock audio, with silence for provider endpointing.
                        data = pcm + bytes(32000)
                        for offset in range(0, len(data), 3200):
                            await ws.send_json({"type": "audio", "audio": base64.b64encode(data[offset:offset + 3200]).decode()})
                            await asyncio.sleep(0.1)

                    sender = asyncio.create_task(stream_pcm())
                    try:
                        async with asyncio.timeout(35):
                            while True:
                                event = await ws.receive_json()
                                assert event.get("type") not in {"error", "provider_unavailable"}, event
                                if event.get("type") not in {"playback_stop", "user_transcript", "speech_control"}:
                                    continue
                                event["atMs"] = round((time.perf_counter() - started) * 1000, 3)
                                row["events"].append(event)
                                if event["type"] == "playback_stop" and event.get("trigger") == "provider-speech-started":
                                    row.setdefault("audioSendToPlaybackStopMs", event["atMs"])
                                if event["type"] == "user_transcript":
                                    row["recognizedText"] = event["text"]
                                if event["type"] == "speech_control":
                                    row["controlMs"] = event["atMs"]
                                    assert event["action"] == ("cancel-answer" if cancel else "stop-speaking"), event
                                    break
                        await sender
                    finally:
                        if not sender.done():
                            sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
                    assert row.get("audioSendToPlaybackStopMs") and row.get("recognizedText"), row
                    state = await (await client.get("/api/state", params={"session": sid})).json()
                    original = next(t for t in state["workLedger"]["turnInbox"] if t["turnId"] == tid)
                    if cancel:
                        assert original["answer"]["status"] == "cancelled", original
                    else:
                        assert original["answer"]["status"] != "cancelled", original
                    entries = journal(output, sid)
                    decisions = [r for r in entries if r["type"] == "control_decision"]
                    assert len(decisions) == 2
                    row["routerMs"] = decisions[-1]["decision"]["latencyMs"]
                    assert not any(r.get("eventType") == "response.dispatched" and
                        (r.get("payload", {}).get("origin") or {}).get("turnId") != tid for r in entries)
                    assert sum(r.get("eventType") == "episodic.memory.retrieved" for r in entries) == 1
                    row["passed"] = True
                    await ws.close()
                    print(json.dumps({k: v for k, v in row.items() if k != "events"}), flush=True)
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        await router.aclose()
    (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: (v[:700] if k == "error" else v) for k, v in report.items() if k not in {"semantic", "audio"}}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--fixture-dir", type=Path, help="Replay existing input-0/1/2.wav fixtures identically across backends")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output, args.text_only, args.fixture_dir)) else 1)
