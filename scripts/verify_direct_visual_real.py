"""Real-provider comparison of visual synthesis and direct visual answer delivery."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
from statistics import median
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


async def verify(output, rounds):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    original_mode = os.environ.get("EGOASSIST_DIRECT_VISUAL_ANSWERS")
    report = {"passed": False, "mock": False, "hardwareMeasured": False, "runs": [],
              "timing": "client WebSocket question send to first output / full provider response; excludes connection setup and physical playback"}
    source = ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4"
    _, image = decode(source)
    report["source"] = str(source)
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/auth/register", json={"username": "visual-latency", "password": "isolated-visual-latency-123"})
            for index in range(rounds):
                for mode in (("synthesis", "direct") if index % 2 == 0 else ("direct", "synthesis")):
                    os.environ["EGOASSIST_DIRECT_VISUAL_ANSWERS"] = "1" if mode == "direct" else "0"
                    sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
                    ws, _ = await connect(client, sid)
                    row = {"mode": mode, "round": index + 1, "sessionId": sid, "passed": False}
                    report["runs"].append(row)
                    try:
                        await ws.send_json({"type": "video", "image": base64.b64encode(image).decode()})
                        result = await turn(ws, output, sid, "请描述摄像头中桌布的颜色和纹理，不要猜测姓名。")
                        row.update(result)
                        assert "格" in result["transcript"] and any(c in result["transcript"] for c in ("红", "橙", "粉")), result
                        rows = journal(output, sid)
                        row["vision"] = [{k: r.get(k) for k in ("type", "latencyMs", "model", "requestId", "usage", "description")}
                            for r in rows if r.get("type") == "scene.question.result"]
                        assert row["vision"] and row["vision"][0]["requestId"]
                        row["routerMs"] = next(r["decision"]["latencyMs"] for r in rows if r.get("type") == "control_decision" and r.get("turnId") == result["turnId"])
                        metrics = [r.get('payload', {}) for r in rows if r.get('eventType') == 'provider.pipeline.metrics']
                        row['calls'] = [{k: m.get(k) for k in ('stage', 'model', 'apiCalls', 'latencyMs')} for m in metrics if m.get('apiCalls', 0) > 0]
                        row['directDispatch'] = any(r.get('payload', {}).get('backend') == 'visual-answer-and-tts' for r in rows)
                        assert row['directDispatch'] == (mode == 'direct'), row
                        assert any(m.get('stage') == 'text' for m in metrics) == (mode == 'synthesis'), row
                        assert any(m.get('stage') == 'tts' for m in metrics), row
                        row["passed"] = True
                    except Exception as exc:
                        row["error"] = type(exc).__name__ + ": " + str(exc)
                    finally:
                        await ws.close()
                    print(json.dumps({k: v for k, v in row.items() if k in ("round", "mode", "passed", "firstAudioMs", "finishedMs", "error")}), flush=True)
                    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
            report["summary"] = {mode: {metric: round(median(r[metric] for r in report["runs"] if r["mode"] == mode and r["passed"]), 1)
                                      for metric in ("firstTextMs", "firstAudioMs", "finishedMs")}
                                 for mode in ("synthesis", "direct") if any(r["passed"] and r["mode"] == mode for r in report["runs"])}
            report["passed"] = all(r["passed"] for r in report["runs"])
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if original_mode is None:
            os.environ.pop("EGOASSIST_DIRECT_VISUAL_ANSWERS", None)
        else:
            os.environ["EGOASSIST_DIRECT_VISUAL_ANSWERS"] = original_mode
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"passed": report["passed"], "summary": report.get("summary")}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 10:
        parser.error("rounds must be between 1 and 10")
    raise SystemExit(0 if asyncio.run(verify(args.output, args.rounds)) else 1)
