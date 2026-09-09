"""Real application test of demand-driven Qwen task observation and usage audit."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from real_session_support import journal, turn
from verify_real_skill_lifecycle import connect
from verify_shared_scene import decode


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "mock": False, "hardwareMeasured": False}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/auth/register", json={"username": "perception", "password": "isolated-perception-123"})).status == 200
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            report["sessionId"] = sid
            ws, state = await connect(client, sid)
            async with ws:
                report["initialLane"] = state["providerLanes"]["perception"]
                assert not report["initialLane"]["connected"] and report["initialLane"]["connectionState"] == "idle"
                report["activation"] = await turn(ws, output, sid, "我要做番茄炒蛋，请简短告诉我第一步。")
                source = ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4"
                _, image = decode(source)
                report["source"] = str(source)
                await ws.send_json({"type": "video", "image": base64.b64encode(image).decode()})
                async with asyncio.timeout(25):
                    while True:
                        rows = journal(output, sid)
                        finished = [r for r in rows if r.get("eventType") == "perception.response.finished"]
                        if finished:
                            break
                        await asyncio.sleep(0.1)
                report["requests"] = [r for r in rows if r.get("eventType") == "perception.response.requested"]
                report["finished"] = finished
                assert len(report["requests"]) == 1
                assert finished[0].get("responseId") and finished[0]["payload"].get("usage")
                assert finished[0]["payload"].get("providerStatus") == "completed"
                report["speakerWarmup"] = [r for r in rows if r.get("type") == "speaker.warmup"]
                report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({k: v for k, v in report.items() if k in {"passed", "error", "initialLane", "speakerWarmup", "finished"}}, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
