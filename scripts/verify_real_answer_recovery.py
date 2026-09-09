"""Interrupt a real grounded answer, restart the server, and verify recovery."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from real_session_support import journal
from verify_real_skill_lifecycle import connect


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "mock": False, "hardwareMeasured": False}
    credentials = {"username": "answer-recovery", "password": "isolated-answer-recovery-123"}

    def app():
        return create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                          "qwen3.5-omni-flash-realtime", "Ethan")

    async def answer(client, sid, tid, expected):
        async with asyncio.timeout(60):
            while True:
                state = await (await client.get("/api/state", params={"session": sid})).json()
                item = next(t for t in state["workLedger"]["turnInbox"] if t["turnId"] == tid)
                result = item.get("answer") or {}
                assert result.get("status") not in {"failed", "cancelled"}, result
                if result.get("status") == expected:
                    return result
                await asyncio.sleep(0.02)

    try:
        async with TestClient(TestServer(app())) as client:
            assert (await client.post("/api/auth/register", json=credentials)).status == 200
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            report["sessionId"] = sid
            ws, _ = await connect(client, sid)
            memory_id = uuid4().hex
            await ws.send_json({"type": "control", "clientRequestId": memory_id,
                                "transcript": "请记住：我的包裹取件码是7392。"})
            async with asyncio.timeout(30):
                while True:
                    event = await ws.receive_json()
                    assert event.get("type") not in {"error", "provider_unavailable"}, event
                    if event.get("type") == "memory_status" and event.get("turnId") == memory_id:
                        assert event["status"] == "committed" and event["committedCardIds"], event
                        report["memoryWrite"] = event
                        break
            tid = uuid4().hex
            await ws.send_json({"type": "control", "clientRequestId": tid,
                "transcript": "我的包裹取件码是多少？先说出取件码，再详细解释怎样保管取件码，至少十句话。"})
            async with asyncio.timeout(45):
                while True:
                    event = await ws.receive_json()
                    assert event.get("type") not in {"error", "provider_unavailable"}, event
                    if event.get("type") == "audio" and event.get("origin", {}).get("turnId") == tid:
                        break
            await ws.send_json({"type": "control", "clientRequestId": uuid4().hex,
                "transcript": "另外一个独立问题：九乘九是多少？"})
            report["beforeShutdown"] = await answer(client, sid, tid, "running")
            before = journal(output, sid)
            assert any(r["type"] == "answer.request" and r.get("turnId") == tid for r in before)
            report["beforeTurnCount"] = sum(r["type"] == "turn_inbox" for r in before)
            await ws.close()
        print("stopped while the real grounded answer request was in flight", flush=True)
        async with TestClient(TestServer(app())) as client:
            assert (await client.post("/api/auth/login", json=credentials)).status == 200
            started = time.perf_counter()
            result = await answer(client, sid, tid, "completed")
            report["recoveryMs"] = round((time.perf_counter() - started) * 1000, 3)
            report["answer"] = result
            assert result["attempt"] >= 2 and result["providerRequestId"] and result["usage"], result
            assert "7392" in result["text"] and result["channel"] == "background-text", result
            after = journal(output, sid)
            assert sum(r["type"] == "turn_inbox" for r in after) == report["beforeTurnCount"] == 3
            assert sum(r["type"] == "control_decision" for r in after) == 3
            assert sum(r["type"] == "answer.result" and r.get("turnId") == tid for r in after) == 1
            assert not any(r["type"] == "tool_audit" for r in after)
            requests = [r for r in after if r["type"] == "answer.request" and r.get("turnId") == tid]
            assert len(requests) >= 2
            report["requestCount"] = len(requests)
            report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"memoryWrite", "answer", "beforeShutdown"}}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
