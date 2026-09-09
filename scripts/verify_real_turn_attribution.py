"""Real Qwen burst, interruption, correction, duplicate and reconnect checks.

Uses actual HTTP/WebSocket providers. Text controls test turn orchestration;
they do not test physical microphone ASR or spoken slip recognition.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from real_session_support import journal
from verify_real_skill_lifecycle import connect


SCENARIOS = ("rapid", "barge-in", "correction", "duplicate-reconnect", "disconnect-inflight", "automatic-completion", "cancellation")


async def verify(output, scenarios=SCENARIOS):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "mock": False, "hardwareMeasured": False, "scenarios": []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                     "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/auth/register", json={"username": "attribution",
                "password": "isolated-attribution-test-123"})).status == 200
            for scenario in scenarios:
                row = {"name": scenario, "passed": False, "events": [], "sent": []}
                report["scenarios"].append(row)
                sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
                row["sessionId"] = sid
                ws, _ = await connect(client, sid)
                started = time.perf_counter()

                async def send(text, tid=None):
                    tid = tid or uuid4().hex
                    row["sent"].append({"turnId": tid, "text": text})
                    await ws.send_json({"type": "control", "transcript": text, "clientRequestId": tid})
                    return tid

                async def until(predicate, timeout=45):
                    async with asyncio.timeout(timeout):
                        while True:
                            msg = await ws.receive()
                            assert msg.type == WSMsgType.TEXT, "socket closed before expected outcome"
                            event = json.loads(msg.data)
                            assert event.get("type") not in {"error", "provider_unavailable"}, event
                            if event.get("type") == "state":
                                continue
                            if event.get("audio"):
                                event["audioBytesBase64"] = len(event.pop("audio"))
                            event["atMs"] = round((time.perf_counter() - started) * 1000, 1)
                            row["events"].append(event)
                            if predicate(event):
                                return event

                if scenario == "duplicate-reconnect":
                    tid = await send("请计算九乘九，只用阿拉伯数字回答结果。")
                    await until(lambda e: e.get("type") == "turn_receipt" and e.get("turnId") == tid)
                    await send(row["sent"][0]["text"], tid)
                    await until(lambda e: e.get("type") == "response_status" and e.get("status") == "completed" and e.get("origin", {}).get("turnId") == tid)
                    await ws.close()
                    await asyncio.sleep(0.5)
                    ws, _ = await connect(client, sid)
                    await send(row["sent"][0]["text"], tid)
                    await until(lambda e: e.get("type") == "turn_receipt" and e.get("deduplicated"))
                    rows = journal(output, sid)
                    assert len([r for r in rows if r.get("type") == "turn_inbox"]) == 1
                    assert sum(e.get("deduplicated") is True for e in row["events"]) == 2
                else:
                    first = await send("请用阿拉伯数字计算七乘八，之后详细解释乘法原理，至少讲十句话。")
                    if scenario == "rapid":
                        await until(lambda e: e.get("type") == "turn_receipt" and e.get("turnId") == first)
                    else:
                        await until(lambda e: e.get("type") == "audio" and e.get("origin", {}).get("turnId") == first)
                    if scenario == "disconnect-inflight":
                        await ws.close()
                        await asyncio.sleep(0.5)
                        ws, _ = await connect(client, sid)
                        await send(row["sent"][0]["text"], first)
                        await until(lambda e: e.get("type") == "turn_receipt" and e.get("deduplicated"))
                    prefix = ("我刚才口误，说错了，撤回七乘八。" if scenario == "correction" else
                              "取消刚才那个问题，不需要再回答七乘八。" if scenario == "cancellation" else
                              "另外一个独立问题：" if scenario == "automatic-completion" else "停下，换一个问题。")
                    tid = await send(prefix + "现在请计算九乘九，只用阿拉伯数字回答结果。")
                    await until(lambda e: e.get("type") == "response_status" and e.get("status") == "completed" and e.get("origin", {}).get("turnId") == tid)
                    snapshot = await (await client.get("/api/state", params={"session": sid})).json()
                    row["turns"] = snapshot["workLedger"]["turnInbox"]
                    old = next(t for t in row["turns"] if t["turnId"] == first)
                    assert (old.get("delivery") or {}).get("status") == "interrupted", old
                    cutoff = next(e["atMs"] for e in row["events"] if e.get("type") == "turn_receipt" and e.get("turnId") == tid)
                    assert not [e for e in row["events"] if e["atMs"] > cutoff and e.get("type") in {"audio", "assistant_transcript"} and e.get("origin", {}).get("turnId") == first]
                    if scenario in {"automatic-completion", "correction", "cancellation"}:
                        expected = "completed" if scenario == "automatic-completion" else "cancelled"
                        async with asyncio.timeout(45):
                            while True:
                                state = await (await client.get("/api/state", params={"session": sid})).json()
                                previous = next(t for t in state["workLedger"]["turnInbox"] if t["turnId"] == first)
                                row["previousAnswer"] = previous.get("answer")
                                if (previous.get("answer") or {}).get("status") == expected:
                                    break
                                assert (previous.get("answer") or {}).get("status") != "failed", previous
                                await asyncio.sleep(0.1)
                        if scenario == "automatic-completion":
                            answer = previous["answer"]
                            assert "56" in answer["text"] and answer["channel"] == "background-text", answer
                            assert answer["providerRequestId"] and answer["usage"], answer
                            assert len([r for r in journal(output, sid) if r.get("type") == "answer.result" and r.get("turnId") == first]) == 1
                spoken = [e for e in row["events"] if e.get("type") == "assistant_transcript" and e.get("done") and e.get("origin", {}).get("turnId") == tid]
                # Audio transcripts can spell the spoken number in Chinese.
                # Preserve formatting compliance separately from answer correctness.
                assert spoken and spoken[-1]["text"].strip().rstrip("。.!！") in {"81", "八十一"}, spoken
                row["arabicNumeralTranscript"] = spoken[-1]["text"].strip() == "81"
                response_id = spoken[-1]["responseId"]
                assert any(e.get("type") == "audio" and e.get("responseId") == response_id and e.get("audioBytesBase64") for e in row["events"])
                assert all(e.get("responseId") and e.get("origin", {}).get("turnId") for e in row["events"] if e.get("type") in {"audio", "assistant_transcript"})
                rows = journal(output, sid)
                row["routing"] = [r["decision"] for r in rows if r.get("type") == "control_decision"]
                assert row["routing"] and all(r["status"] == "classified" for r in row["routing"]), row["routing"]
                row["answer"] = spoken[-1]["text"]
                row["passed"] = True
                await ws.close()
                print(json.dumps({"scenario": scenario, "passed": True, "answer": row["answer"]}), flush=True)
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "scenarios"}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output, (args.scenario,) if args.scenario else SCENARIOS)) else 1)
