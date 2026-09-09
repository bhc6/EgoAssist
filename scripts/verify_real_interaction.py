"""Real Qwen application checks for text continuation and targeted cancellation.

Text inputs exercise actual routing, realtime generation, persistence and workers.
Use verify_real_voice_stop_browser.py for ASR and device playback measurements.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from real_session_support import journal
from verify_real_skill_lifecycle import connect


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "mock": False, "hardwareMeasured": False, "cases": []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                     "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/auth/register", json={"username": "interaction",
                "password": "isolated-interaction-test-123"})).status == 200
            for case in ("text-continuation", "older-target", "quiet-reconnect"):
                row = {"name": case, "passed": False, "events": []}
                report["cases"].append(row)
                sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
                row["sessionId"] = sid
                ws, _ = await connect(client, sid)

                async def send(text):
                    tid = uuid4().hex
                    await ws.send_json({"type": "control", "clientRequestId": tid, "transcript": text})
                    return tid

                async def until(predicate):
                    async with asyncio.timeout(60):
                        while True:
                            msg = await ws.receive()
                            assert msg.type == WSMsgType.TEXT, "Socket closed"
                            event = json.loads(msg.data)
                            assert event.get("type") not in {"error", "provider_unavailable"}, event
                            if "audio" in event:
                                event["audioBytesBase64"] = len(event.pop("audio"))
                            if event.get("type") != "state":
                                row["events"].append(event)
                            if predicate(event):
                                return event

                original = await send("请用阿拉伯数字计算七乘八，然后详细解释乘法原理，至少讲二十句话。")
                row["originalTurnId"] = original
                await until(lambda e: e.get("type") == "audio" and e.get("origin", {}).get("turnId") == original)
                if case == "text-continuation":
                    control = await send("别念出来，继续整理好后放到屏幕上。")
                    await until(lambda e: e.get("type") == "speech_control" and e.get("turnId") == control)
                    async with asyncio.timeout(60):
                        while True:
                            state = await (await client.get("/api/state", params={"session": sid})).json()
                            answer = next(t for t in state["workLedger"]["turnInbox"] if t["turnId"] == original)["answer"]
                            assert answer["status"] not in {"cancelled", "failed"}, answer
                            if answer["status"] == "completed" and answer.get("channel") == "background-text":
                                break
                            await asyncio.sleep(0.2)
                    assert "56" in answer["text"] and answer.get("providerRequestId"), answer
                    assert len([t for t in state["workLedger"]["turnInbox"] if t.get("answer")]) == 1
                    row["answer"] = answer
                elif case == "older-target":
                    newer = await send("另外一个独立问题，请详细解释光合作用，至少讲二十句话。")
                    await until(lambda e: e.get("type") == "audio" and e.get("origin", {}).get("turnId") == newer)
                    control = await send("七乘八和乘法原理那个旧问题我不需要答案了，请取消那个答案。光合作用的问题要保留，但先别念。")
                    await until(lambda e: e.get("type") == "speech_control" and e.get("turnId") == control)
                    state = await (await client.get("/api/state", params={"session": sid})).json()
                    answers = {t["turnId"]: t.get("answer") for t in state["workLedger"]["turnInbox"]}
                    assert answers[original]["status"] == "cancelled", answers
                    assert answers[newer]["status"] != "cancelled", answers
                    assert answers[control] is None, answers
                    row["answers"] = answers
                else:
                    control = await send("接下来五分钟不要主动说话，等我问你再回答。")
                    await until(lambda e: e.get("type") == "speech_control" and e.get("turnId") == control)
                    state = await (await client.get("/api/state", params={"session": sid})).json()
                    permission = state["interaction"]
                    assert permission["quiet"] and permission["until"] and permission["allowReplies"], permission
                    await ws.close()
                    # Mirror the browser's bounded retry while the old socket releases ownership.
                    for attempt in range(4):
                        try:
                            ws, _ = await connect(client, sid)
                            break
                        except WSServerHandshakeError as exc:
                            row.setdefault("reconnectConflicts", []).append(exc.status)
                            if exc.status != 409 or attempt == 3:
                                raise
                            await asyncio.sleep(0.5 * 2 ** attempt)
                    state = await (await client.get("/api/state", params={"session": sid})).json()
                    assert state["interaction"]["until"] == permission["until"]
                    question = await send("现在我问你，九乘九是多少？只回答结果。")
                    await until(lambda e: e.get("type") == "audio" and e.get("origin", {}).get("turnId") == question)
                    state = await (await client.get("/api/state", params={"session": sid})).json()
                    assert state["interaction"]["quiet"]
                    row["permission"] = state["interaction"]
                row["decisions"] = [r["decision"] for r in journal(output, sid) if r["type"] == "control_decision"]
                assert row["decisions"] and all(d["status"] == "classified" for d in row["decisions"])
                row["passed"] = True
                await ws.close()
                print(json.dumps({"case": case, "passed": True}), flush=True)
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
