"""Probe the production Qwen router without changing its prompt or hiding failures.

The default text probe does not measure VAD, ASR, playback, speaker attribution,
or real human interaction. Optional audio mode exercises real Qwen TTS and ASR,
but not physical hardware. Cases with missing product capabilities remain listed
even if their available protocol assertions pass.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egoassist._env import load_env
from egoassist.realtime import SemanticTurnRouter


def assess(case, decision):
    revision = decision.get("revision") or {}
    actual = {"stop": decision.get("speechControl") == "stop-speaking",
              "cancel": revision.get("type") == "cancellation",
              "withdrawn": bool(revision)}
    failures = [key for key in actual if key in case and actual[key] != case[key]]
    # A correction also withdraws the old answer; protecting it must cover both.
    if case.get("cancel") is False and revision and "cancel" not in failures:
        failures.append("unexpected-revision")
    if decision["status"] != "classified":
        failures.append("classification")
    return actual, failures


def summarize(report, output):
    assessed = [row for row in report["results"] if row["hasAssertions"]]
    latencies = [row["decision"]["latencyMs"] for row in report["results"]]
    summary = {"totalRequests": len(report["results"]), "assertedRequests": len(assessed),
               "assertionsPassed": sum(row["assertionsPassed"] for row in assessed),
               "failedCases": [{"id": row["id"], "repeat": row["repeat"], "failures": row["failures"]}
                               for row in report["results"] if not row["assertionsPassed"]],
               "classificationFailures": sum(row["decision"]["status"] != "classified" for row in report["results"]),
               "medianRouterMs": statistics.median(latencies), "maxRouterMs": max(latencies),
               "productAccepted": False,
               "limitation": "Partial protocol assertions are not natural interaction acceptance; see unsupported requirements and raw decisions."}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return not summary["failedCases"]


def reanalyze(recorded, cases_path, output):
    report = json.loads(recorded.read_text(encoding="utf-8"))
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in fixture["cases"]}
    report["originalFixture"] = report["fixture"]
    report["fixture"] = fixture
    report["reanalysisOf"] = str(recorded)
    for row in report["results"]:
        actual, failures = assess(cases[row["id"]], row["decision"])
        row.update(failures=failures, assertionsPassed=not failures,
                   hasAssertions=any(key in cases[row["id"]] for key in actual))
    output.mkdir(parents=True, exist_ok=False)
    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return summarize(report, output)


async def research(cases_path, output, repeats):
    load_env(str(ROOT / ".env"))
    api_key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("QWEN_API_KEY is required; no offline fallback")
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"createdAt": datetime.now(timezone.utc).isoformat(), "mock": False,
              "scope": "real-provider-text-router-only", "hardwareMeasured": False,
              "fixture": fixture, "repeats": repeats, "results": []}
    router = SemanticTurnRouter(api_key)
    try:
        for repeat in range(repeats):
            for case in fixture["cases"]:
                context = case.get("context", fixture["context"])
                router.set_turn_context(clock_context=None, memory_draft=None,
                    previous_turn=context.get("previousTurn"),
                    previous_answer=context.get("previousAnswer"))
                decision = await router.classify(case["text"], [])
                actual, failures = assess(case, decision)
                row = {"id": case["id"], "repeat": repeat + 1, "decision": decision,
                       "assertionsPassed": not failures, "failures": failures,
                       "hasAssertions": any(key in case for key in actual),
                       "unsupportedRequirement": case.get("unsupportedRequirement")}
                report["results"].append(row)
                (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"id": case["id"], "repeat": repeat + 1, "actual": actual,
                                  "failures": failures, "latencyMs": decision["latencyMs"]}), flush=True)
    finally:
        await router.aclose()
    return summarize(report, output)


async def research_audio(cases_path, output, case_ids):
    import base64
    import time
    from uuid import uuid4
    from aiohttp import WSMsgType
    from aiohttp.test_utils import TestClient, TestServer
    from egoassist import BUILTIN_SKILLS_ROOT
    from run_egoassist import create_app
    from real_session_support import journal
    from verify_real_skill_lifecycle import connect
    from verify_real_voice_stop import synthesize

    load_env(str(ROOT / ".env"))
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in fixture["cases"]}
    if not set(case_ids).issubset(cases):
        raise ValueError("unknown audio case")
    output.mkdir(parents=True, exist_ok=False)
    report = {"scope": "real-Qwen-TTS-ASR-application", "mock": False,
              "hardwareMeasured": False, "productAccepted": False, "cases": []}
    try:
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                         "qwen3.5-omni-flash-realtime", "Ethan")
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/auth/register", json={
                "username": "interruption-research", "password": "isolated-research-123"})).status == 200
            for index, case_id in enumerate(case_ids):
                case = cases[case_id]
                pcm, audio_fixture = await synthesize(case["text"], output, index)
                sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
                ws, _ = await connect(client, sid)
                turn_id = uuid4().hex
                await ws.send_json({"type": "control", "clientRequestId": turn_id,
                    "transcript": "请详细解释乘法原理，至少讲二十句话。"})
                async with asyncio.timeout(45):
                    while True:
                        event = await ws.receive_json()
                        assert event.get("type") not in {"error", "provider_unavailable"}, event
                        if event.get("type") == "audio" and event.get("origin", {}).get("turnId") == turn_id:
                            break
                row = {"id": case_id, "text": case["text"], "sessionId": sid,
                       "originalTurnId": turn_id, "fixture": audio_fixture, "events": []}
                report["cases"].append(row)
                started = time.perf_counter()

                async def send_audio():
                    data = pcm + bytes(32000)
                    for offset in range(0, len(data), 3200):
                        await ws.send_json({"type": "audio", "audio": base64.b64encode(data[offset:offset + 3200]).decode()})
                        await asyncio.sleep(0.1)

                sender = asyncio.create_task(send_audio())
                try:
                    # Keep collecting beyond endpointing to observe split transcripts
                    # and any later speech, instead of accepting the first stop event.
                    deadline = started + len(pcm) / 32000 + 12
                    while time.perf_counter() < deadline:
                        try:
                            message = await ws.receive(timeout=0.25)
                        except asyncio.TimeoutError:
                            continue
                        assert message.type == WSMsgType.TEXT, "WebSocket closed during observation"
                        event = json.loads(message.data)
                        assert event.get("type") not in {"error", "provider_unavailable"}, event
                        if event.get("type") in {"playback_stop", "speech_control", "user_transcript", "response_status"}:
                            row["events"].append({**event, "atMs": round((time.perf_counter() - started) * 1000, 2)})
                    await sender
                finally:
                    if not sender.done():
                        sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                entries = journal(output, sid)
                row["decisions"] = [r for r in entries if r["type"] == "control_decision"]
                row["responses"] = [r for r in entries if r.get("eventType") == "response.dispatched"]
                row["observedMs"] = round((time.perf_counter() - started) * 1000, 2)
                row["workLedger"] = (await (await client.get("/api/state", params={"session": sid})).json())["workLedger"]
                await ws.close()
                (output / "audio-research.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"id": case_id, "controls": [e for e in row["events"] if e["type"] == "speech_control"],
                    "decisions": [{"text": r["input"], "raw": r["decision"].get("rawOutput")} for r in row["decisions"]]}), flush=True)
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "audio-research.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scope": report["scope"], "cases": len(report["cases"]), "error": report.get("error")}), flush=True)
    return "error" not in report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "scripts/fixtures/interruption_cases.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--audio-cases", nargs="+", help="Run selected cases through actual Qwen TTS and realtime ASR instead of text probes")
    parser.add_argument("--recorded", type=Path, help="Reassess saved real decisions against current expectations; makes no model requests")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.recorded:
        if args.audio_cases:
            parser.error("--recorded and --audio-cases cannot be combined")
        raise SystemExit(0 if reanalyze(args.recorded, args.cases, args.output) else 1)
    job = research_audio(args.cases, args.output, args.audio_cases) if args.audio_cases else research(args.cases, args.output, args.repeats)
    raise SystemExit(0 if asyncio.run(job) else 1)
