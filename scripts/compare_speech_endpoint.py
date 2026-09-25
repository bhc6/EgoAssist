"""Alternate real Omni endpoint settings using identical synthetic PCM fixtures.

Measures PCM input end to first received audio, not physical mouth-to-ear latency.
All trials, including failures, are retained. No microphone or AEC is measured.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from verify_real_skill_lifecycle import connect
from verify_real_voice_stop import decode_fixture, synthesize


async def asr_trial(pcm, row):
    from egoassist.realtime.omni import OmniBrowserBridge

    started = None
    async def emit(event):
        if started is None:
            return
        elapsed = (time.perf_counter() - started) * 1000 - len(pcm) / 32
        if event.get("type") == "user_transcript":
            row["transcripts"].append(event["text"])
            row.setdefault("transcriptMs", elapsed)
        if event.get("type") in {"error", "provider_unavailable"}:
            row["error"] = event.get("message") or event.get("reason")

    bridge = OmniBrowserBridge(os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"),
                              "qwen3.5-omni-flash-realtime", "Ethan", "Transcribe the input.", emit, lambda text: None)
    bridge.interaction_managed = True
    try:
        await bridge.connect()
        started = time.perf_counter()
        data = pcm + bytes(32000 * 3)
        for offset in range(0, len(data), 3200):
            await asyncio.sleep(max(0, started + offset / 32000 - time.perf_counter()))
            bridge.append_audio(base64.b64encode(data[offset:offset+3200]).decode())
        async with asyncio.timeout(10):
            while not row["transcripts"] and not row.get("error"):
                await asyncio.sleep(.02)
        expected = ("七加八", "7加8", "7+8") if row["case"] == "short" else ("十九加二十三", "19加23", "19+23")
        row["passed"] = (len(row["transcripts"]) == 1 and not row.get("error")
                         and any(value in row["transcripts"][0] for value in expected))
        row["provider"] = bridge.provider_lanes_snapshot()
    finally:
        await bridge.close()


async def compare(output, repeats, fixture_dir=None, candidate_ms=1000, asr_only=False):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "realProviders": True, "hardwareMeasured": False,
              "clock": "PCM input end to transcript" if asr_only else "PCM input end to first received audio",
              "runs": [], "sources": {},
              "baselineMs": 1200, "candidateMs": candidate_ms,
              "asrOnly": asr_only, "answerLatencyMeasured": not asr_only,
              "sourceHashes": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                               for path in ("egoassist/realtime/omni.py", "egoassist/prompts/decisions_router.json")}}
    previous = {key: os.environ.get(key) for key in
                ("EGOASSIST_REALTIME_BACKEND", "EGOASSIST_SPEECH_END_SILENCE_MS")}
    os.environ["EGOASSIST_REALTIME_BACKEND"] = "omni"

    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        phrases = ["助手，七加八是多少？请直接回答结果。", "助手，请帮我算一下，",
                   "十九加二十三是多少？请直接回答结果。"]
        clips = []
        for index, phrase in enumerate(phrases):
            if fixture_dir:
                pcm, metadata = decode_fixture((fixture_dir / f"input-{index}.wav").read_bytes())
            else:
                pcm, metadata = await synthesize(phrase, output, index)
            clips.append(pcm)
            report["sources"][str(index)] = {"text": phrase, "kind": "synthetic-speech", **metadata}
        cases = [("short", clips[0], ("15", "十五")),
                 ("pause-500ms", clips[1] + bytes(16000) + clips[2], ("42", "四十二"))]
        report["fixtures"] = {name: {"pcmSha256": hashlib.sha256(pcm).hexdigest(), "durationMs": len(pcm) / 32}
                              for name, pcm, _ in cases}
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                         "qwen3.5-omni-flash-realtime", "Ethan")
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/auth/register", json={
                "username": "endpoint-comparison", "password": "isolated-endpoint-test-123"})
            assert response.status == 200
            for repeat in range(repeats):
                for name, pcm, expected in cases:
                    for silence in ([1200, candidate_ms] if repeat % 2 == 0 else [candidate_ms, 1200]):
                        os.environ["EGOASSIST_SPEECH_END_SILENCE_MS"] = str(silence)
                        sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
                        row = {"case": name, "repeat": repeat, "silenceMs": silence, "sessionId": sid,
                               "passed": False, "transcripts": [], "events": []}
                        report["runs"].append(row)
                        if asr_only:
                            try:
                                await asr_trial(pcm, row)
                            except Exception as exc:
                                row["error"] = f"{type(exc).__name__}: {exc}"
                            save()
                            print(json.dumps({k: v for k, v in row.items() if k not in {"events", "provider"}}, ensure_ascii=True), flush=True)
                            continue
                        ws = None
                        sender = None
                        try:
                            ws, _ = await connect(client, sid)
                            started = time.perf_counter()

                            async def send_audio():
                                data = pcm + bytes(32000 * 3)
                                for offset in range(0, len(data), 3200):
                                    await asyncio.sleep(max(0, started + offset / 32000 - time.perf_counter()))
                                    await ws.send_json({"type": "audio", "audio": base64.b64encode(data[offset:offset+3200]).decode()})

                            sender = asyncio.create_task(send_audio())
                            async with asyncio.timeout(45):
                                while True:
                                    event = await ws.receive_json()
                                    kind = event.get("type")
                                    elapsed = (time.perf_counter() - started) * 1000 - len(pcm) / 32
                                    if kind in {"error", "provider_unavailable"}:
                                        raise RuntimeError(str(event.get("message") or event.get("reason")))
                                    if kind == "user_transcript":
                                        row["transcripts"].append(event.get("text") or event.get("transcript"))
                                        row.setdefault("transcriptMs", elapsed)
                                    if kind == "audio":
                                        row.setdefault("firstAudioMs", elapsed)
                                    if kind == "assistant_transcript" and event.get("done"):
                                        row["answer"] = event.get("text", "")
                                    if kind in {"user_transcript", "speech_floor", "speech_control", "response_status"}:
                                        row["events"].append({**event, "inputEndMs": elapsed})
                                    if kind == "response_status" and event.get("status") == "completed" and row.get("answer"):
                                        await sender
                                        row["passed"] = ("firstAudioMs" in row and len(row["transcripts"]) == 1
                                                         and any(value in row["answer"] for value in expected))
                                        break
                        except Exception as exc:
                            row["error"] = f"{type(exc).__name__}: {exc}"
                        finally:
                            if sender:
                                sender.cancel()
                                await asyncio.gather(sender, return_exceptions=True)
                            if ws:
                                await ws.close()
                            paths = list(output.glob("users/*/sessions/" + sid + ".jsonl"))
                            if paths:
                                entries = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines() if line.strip()]
                                row["routing"] = [{key: entry["decision"].get(key) for key in ("status", "latencyMs", "modelsUsed", "providerError")}
                                                  for entry in entries if entry.get("type") == "control_decision"]
                                row["provider"] = [entry["payload"] for entry in entries if entry.get("eventType") == "provider.session.ready"]
                                row["passed"] = (row["passed"] and bool(row["routing"])
                                                 and all(item["status"] == "classified" for item in row["routing"]))
                            save()
                        print(json.dumps({k: v for k, v in row.items() if k != "events"}, ensure_ascii=True), flush=True)
                        if any((item.get("providerError") or {}).get("accountBlocked") for item in row.get("routing", [])):
                            report["blocked"] = "Provider account access or credits unavailable; remaining trials were not run."
                            save()
                            return False
        report["passed"] = all(row["passed"] for row in report["runs"])
        report["summary"] = {}
        for silence in (1200, candidate_ms):
            rows = [row for row in report["runs"] if row["silenceMs"] == silence]
            report["summary"][str(silence)] = {"n": len(rows), "passed": sum(row["passed"] for row in rows)}
            for metric in ("transcriptMs", "firstAudioMs"):
                values = [row[metric] for row in rows if metric in row]
                report["summary"][str(silence)][metric + "Median"] = statistics.median(values) if values else None
        save()
        print(json.dumps(report["summary"]), flush=True)
        return report["passed"]
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--candidate-ms", type=int, choices=(800, 1000), default=1000)
    parser.add_argument("--asr-only", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    raise SystemExit(0 if asyncio.run(compare(args.output, args.repeats, args.fixture_dir, args.candidate_ms, args.asr_only)) else 1)
