"""Exercise Skill workflows through production HTTP/WS and real Qwen providers.

Text controls measure routing and response audio, not microphone or physical work.
The step confirmations below are test-user declarations, not camera evidence.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


from real_session_support import journal, turn


async def verify(output, model):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"mock": False, "model": model, "input": "text-control", "physicalCompletionMeasured": False,
              "passed": False, "turns": []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), model, "Ethan")
    scenarios = [
        ("我要做番茄炒蛋，请告诉我第一步。", "kitchen-coach", "tomato-eggs", "prepare", False),
        ("请重复当前步骤。", "kitchen-coach", "tomato-eggs", "prepare", False),
        ("暂停当前任务。", "kitchen-coach", "tomato-eggs", "prepare", True),
        ("继续当前任务。", "kitchen-coach", "tomato-eggs", "prepare", False),
        ("我确认已经准备好鸡蛋、番茄、油和盐，这一步完成了。", "kitchen-coach", "tomato-eggs", "wash", False),
        ("回到上一步。", "kitchen-coach", "tomato-eggs", "prepare", False),
        ("我要组装凳子，请告诉我第一步。", "assembly-coach", "flat-pack-stool", "inventory", False),
        ("请展示当前步骤的参考资料。", "assembly-coach", "flat-pack-stool", "inventory", False),
    ]
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/auth/register", json={"username": "skill-acceptance",
                                         "password": "isolated-skill-test-123"})
            assert response.status == 200
            response = await client.post("/api/sessions")
            sid = (await response.json())["session"]["id"]
            report["sessionId"] = sid
            async with client.ws_connect("/ws/realtime?session=" + sid) as ws:
                async with asyncio.timeout(30):
                    while True:
                        payload = await ws.receive_json()
                        if payload.get("type") == "ready":
                            assert payload.get("lanes", {}).get("dialogue", {}).get("connected")
                            break
                        assert payload.get("type") not in {"error", "provider_unavailable"}, payload
                for text, skill, task, checkpoint, paused in scenarios:
                    result = await turn(ws, output, sid, text)
                    response = await client.get("/api/state?session=" + sid)
                    state = await response.json()
                    task_state = state.get("task") or {}
                    result["state"] = {"activeSkill": (state.get("activeSkill") or {}).get("name"),
                                       "toolAudit": state.get("toolAudit"),
                                       **{key: task_state.get(key) for key in ("task", "currentCheckpointId", "paused", "stepIndex")}}
                    report["turns"].append(result)
                    assert (state.get("activeSkill") or {}).get("name") == skill, result["state"]
                    assert task_state["task"]["id"] == task, result["state"]
                    assert task_state["currentCheckpointId"] == checkpoint, result["state"]
                    assert task_state["paused"] == paused, result["state"]
                    result["passed"] = True
                references = [item for item in state.get("toolAudit", []) if item.get("tool") == "show_reference"
                              and item.get("result", {}).get("status") == "resolved"]
                assert references, "Reference request did not execute the show_reference tool"
                artifact = references[0]["result"]["artifact"]
                response = await client.get(artifact["previewUrl"], params={"session": sid})
                assert response.status == 200, "Resolved artifact cannot be accessed"
                assert (await response.read()).startswith(b"%PDF"), "Reference is not a PDF"
                report["referenceVerified"] = True
                rows = journal(output, sid)
                report["routing"] = [r["decision"] for r in rows if r.get("type") == "control_decision"]
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
