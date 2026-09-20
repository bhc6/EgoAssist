"""Real application text turns: no microphone, audio frames, or synthetic speech."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import time
from uuid import uuid4
import wave

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(output):
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "realModels": True, "inputAudioFrames": 0, "turns": []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                     "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestClient(TestServer(app)) as client:
            login = await client.post("/api/auth/register", json={"username": "text-only", "password": "text-only-verification-123"})
            assert login.status == 200
            sid = (await (await client.post("/api/sessions", json={"label": "No microphone text verification"})).json())["session"]["id"]
            report["sessionId"] = sid
            async with client.ws_connect("/ws/realtime?session=" + sid) as ws:
                async with asyncio.timeout(60):
                    while (await ws.receive_json()).get("type") != "ready":
                        pass
                for question, expected in [
                    ("请用阿拉伯数字简短回答：二加三等于多少？", ["5", "五"]),
                    ("把你刚才算出的结果乘以四，等于多少？请用阿拉伯数字简短回答。", ["20", "二十"]),
                ]:
                    tid, started, pcm = uuid4().hex, time.perf_counter(), bytearray()
                    row = {"turnId": tid, "question": question, "expected": expected, "events": []}
                    report["turns"].append(row)
                    await ws.send_json({"type": "control", "clientRequestId": tid, "transcript": question})
                    async with asyncio.timeout(70):
                        while True:
                            message = await ws.receive()
                            assert message.type == WSMsgType.TEXT, "socket closed"
                            event = json.loads(message.data)
                            kind = event.get("type")
                            elapsed = round((time.perf_counter() - started) * 1000, 1)
                            if kind == "state":
                                continue
                            if "audio" in event:
                                chunk = base64.b64decode(event.pop("audio"))
                                event["audioBytes"] = len(chunk)
                                pcm.extend(chunk)
                                row.setdefault("firstAudioMs", elapsed)
                            row["events"].append({"elapsedMs": elapsed, **event})
                            assert kind not in {"error", "provider_unavailable"}, event
                            if kind == "assistant_transcript" and event.get("origin", {}).get("turnId") == tid:
                                row.setdefault("firstTextMs", elapsed)
                                row["answer"] = event["text"] if event.get("done") else row.get("answer", "") + event.get("text", "")
                            if kind == "response_status" and event.get("origin", {}).get("turnId") == tid and event.get("status") in {"completed", "interrupted"}:
                                assert event["status"] == "completed", event
                                break
                    row["durationMs"] = round((time.perf_counter() - started) * 1000, 1)
                    row["audioBytes"] = len(pcm)
                    assert any(value in row.get("answer", "") for value in expected), row
                    assert pcm, "no synthesized output audio"
                    with wave.open(str(output / (tid + ".wav")), "wb") as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(24000)
                        audio.writeframes(pcm)
                    state = await (await client.get("/api/state", params={"session": sid})).json()
                    turn = next(t for t in state["workLedger"]["turnInbox"] if t["turnId"] == tid)
                    row["persistedAnswer"] = turn.get("answer")
                    assert row["persistedAnswer"]["status"] == "completed"
                    assert row["persistedAnswer"]["text"] == row["answer"]
                    assert row["persistedAnswer"]["model"] != "qwen3.5-omni-flash-realtime"
                    row["passed"] = True
                    print(json.dumps({key: value for key, value in row.items() if key != "events"}, ensure_ascii=True), flush=True)
                report["passed"] = True
    finally:
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_env()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
