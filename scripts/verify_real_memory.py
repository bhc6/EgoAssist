"""Real Qwen memory write, fresh-session recall, and use with journal evidence."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from real_session_support import journal, turn


async def ready(ws):
    async with asyncio.timeout(30):
        while True:
            message = await ws.receive_json()
            assert message.get("type") not in {"error", "provider_unavailable"}, message
            if message.get("type") == "ready":
                assert message.get("lanes", {}).get("dialogue", {}).get("connected")
                return


async def verify(output, model):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    value = str(int(uuid4().hex[:8], 16) % 900000 + 100000)
    report = {"passed": False, "mock": False, "model": model, "input": "text-control",
              "freshSession": True, "value": value, "turns": []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), model, "Ethan")
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/auth/register", json={"username": "memory-fresh",
                                         "password": "isolated-memory-test-123"})
            assert response.status == 200
            response = await client.post("/api/sessions")
            sid_a = (await response.json())["session"]["id"]
            report["writeSession"] = sid_a
            async with client.ws_connect("/ws/realtime?session=" + sid_a) as ws:
                await ready(ws)
                start = time.perf_counter()
                await ws.send_json({"type": "control", "transcript": f"请记住：我的储物柜编号是{value}。"})
                async with asyncio.timeout(30):
                    while True:
                        message = await ws.receive_json()
                        assert message.get("type") not in {"error", "provider_unavailable"}, message
                        if message.get("type") == "memory_status":
                            report["receipt"] = message
                            report["receiptMs"] = round((time.perf_counter() - start) * 1000, 1)
                            assert message.get("status") == "committed", message
                            assert value in message.get("message", ""), message
                            assert message.get("committedCardIds"), message
                            break
            # No conversation or value is copied into this second provider session.
            response = await client.post("/api/sessions")
            sid_b = (await response.json())["session"]["id"]
            report["recallSession"] = sid_b
            assert sid_a != sid_b
            async with client.ws_connect("/ws/realtime?session=" + sid_b) as ws:
                await ready(ws)
                for text in ("我的储物柜编号是什么？", "请用刚才查到的编号，写一句我可以发给前台的取物说明。"):
                    result = await turn(ws, output, sid_b, text)
                    report["turns"].append(result)
                    assert value in result["transcript"], result
                    response = await client.get("/api/state", params={"session": sid_b})
                    state = await response.json()
                    assert state.get("activeSkill") is None, "Memory dialogue activated an unrelated Skill"
            write_rows = journal(output, sid_a)
            read_rows = journal(output, sid_b)
            committed = set(report["receipt"]["committedCardIds"])
            recalls = [r["payload"] for r in read_rows if r.get("eventType") == "episodic.memory.retrieved"]
            assert any(c.get("selected") and c.get("episodeId") in committed
                       for row in recalls for c in row.get("candidates", []))
            report["retrievalMs"] = [r.get("retrievalMs") for r in recalls]
            report["writePolicyMs"] = [r["payload"].get("writePolicyMs") for r in write_rows
                                       if r.get("eventType") == "timeline.memory.written"]
            report["routing"] = [r["decision"] for r in write_rows + read_rows if r.get("type") == "control_decision"]
            report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.5-omni-flash-realtime")
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(verify(args.output, args.model)) else 1)
