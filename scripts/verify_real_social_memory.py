"""Real-provider, fresh-session social and multi-time memory acceptance."""

import argparse
import asyncio
from datetime import timedelta
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
from egoassist.memory import ClockContext
from run_egoassist import create_app
from real_session_support import journal, turn
from verify_real_memory import ready


async def configure(ws):
    await ready(ws)
    await ws.send_json({"type": "media_config", "clock": {"timezone": "Asia/Shanghai", "utcOffsetMinutes": 480}})


async def save(ws, text):
    started = time.perf_counter()
    await ws.send_json({"type": "control", "transcript": text})
    async with asyncio.timeout(35):
        while True:
            message = await ws.receive_json()
            assert message.get("type") not in {"error", "provider_unavailable"}, message
            if message.get("type") == "memory_status":
                assert message.get("status") == "committed", message
                return {"input": text, "receipt": message,
                        "receiptMs": round((time.perf_counter() - started) * 1000, 1)}


async def verify(output, relative_anchor=False):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    clock = ClockContext.current("Asia/Shanghai")
    yesterday = (clock.local_now.date() - timedelta(days=1)).isoformat()
    before = (clock.local_now.date() - timedelta(days=3 if relative_anchor else 2)).isoformat()
    person_a, person_b, recipient, scene = (("陈澄", "周岳", "苏岩", "实验室") if relative_anchor
                                            else ("张岚", "李宁", "王梅", "咖啡馆"))
    code = "Q" + uuid4().hex[:6].upper()
    model = "qwen3.5-omni-flash-realtime"
    report = {"passed": False, "mock": False, "input": "text-control", "model": model,
              "clock": clock.prompt_payload(), "testCode": code, "writes": [], "turns": []}
    report["relativeAnchor"] = relative_anchor
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), model, "Ethan")
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/auth/register", json={"username": "social-memory-a", "password": "isolated-social-memory-123"})
            assert response.status == 200
            report["userId"] = (await response.json())["user"]["id"]
            response = await client.post("/api/sessions")
            first = (await response.json())["session"]["id"]
            report["writeSession"] = first
            texts = [
                f"请记住：{yesterday}{person_a}和{person_b}在{scene}讨论了{before}的约定：由{person_b}把编号{code}的青色样机交给{recipient}。",
                f"请记住：{before}{person_a}和{person_b}在{scene}讨论了{yesterday}的约定：由{person_a}把红色文件交给赵宇。",
            ]
            async with client.ws_connect('/ws/realtime?session=' + first) as ws:
                await configure(ws)
                for text in texts:
                    report["writes"].append(await save(ws, text))
            response = await client.post("/api/sessions")
            second = (await response.json())["session"]["id"]
            report["recallSession"] = second
            async with client.ws_connect('/ws/realtime?session=' + second) as ws:
                await configure(ws)
                anchor_text = "、以那次讨论为参照的前天" if relative_anchor else "前天"
                result = await turn(ws, output, second, f"{person_a}和{person_b}昨天在{scene}讨论的{anchor_text}的约定是什么？请简短回答。")
                report["turns"].append(result)
                assert code in result["transcript"] and person_b in result["transcript"] and recipient in result["transcript"], result
                assert "红色" not in result["transcript"] and "赵宇" not in result["transcript"], result
                state = await (await client.get('/api/state', params={"session": second})).json()
                report["performance"] = state["performance"]
                evidence = state["episodicMemory"]["retrieval"]["injection"]["evidence"]
                report["injectedEvidence"] = evidence
                assert any(code in item["summary"] and len(item["evidenceContext"].get("times", [])) == 2 for item in evidence)
                assert not any("红色" in item["summary"] for item in evidence)
                assert state["performance"]["metrics"]["responseFirstAudioMs"]["responseId"]
            exported = await (await client.get('/api/privacy/export')).json()
            assert exported["complete"] is True
            cards = exported["timelineMemory"]["cards"]
            journal_ids = {entry["id"] for item in exported["journals"] for entry in item["entries"] if "id" in entry}
            target_cards = [card for card in cards if code in card["text"]]
            assert target_cards and all(card["sourceIds"] and set(card["sourceIds"]) <= journal_ids for card in target_cards)
            report["exportAudit"] = {"complete": exported["complete"], "cards": target_cards,
                                     "journalSessions": [item["sessionId"] for item in exported["journals"]],
                                     "sourceReferencesResolved": True}
            response = await client.post('/api/auth/register', json={"username": "social-memory-b", "password": "isolated-social-memory-123"})
            assert response.status == 200
            other_export = await (await client.get('/api/privacy/export')).json()
            assert other_export["timelineMemory"]["cards"] == [] and other_export["journals"] == []
            report["otherUserIsolated"] = True
            report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["routing"] = [row["decision"] for sid in (report.get("writeSession"), report.get("recallSession")) if sid
                         for row in journal(output, sid) if row.get("type") == "control_decision"]
    (output / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in {"routing", "exportAudit", "injectedEvidence"}}, ensure_ascii=True), flush=True)
    return report["passed"]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--relative-anchor', action='store_true')
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(verify(args.output, args.relative_anchor)) else 1)
