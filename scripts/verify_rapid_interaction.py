"""Real provider rapid text turns, foreground audio and durable background answers.

Each interval uses a fresh isolated session. No mocked responses or device claims.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from real_session_support import journal
from run_egoassist import create_app
from verify_real_skill_lifecycle import connect


CASES = [
    ("只回答数字：十七加二十八等于多少？", "45"),
    ("只回答数字：十九乘四等于多少？", "76"),
    ("只回答数字：八十三减二十六等于多少？", "57"),
]
CHINESE_ANSWERS = {"45": "四十五", "76": "七十六", "57": "五十七"}


async def run_case(client, output, interval_ms, args):
    result = {"intervalMs": interval_ms, "passed": False, "turns": [], "errors": []}
    sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
    result["sessionId"] = sid
    ws = None
    reader = None
    started = time.perf_counter()
    now = lambda: round((time.perf_counter() - started) * 1000, 1)
    rows = {}
    try:
        ws, _ = await connect(client, sid)
        result["readyMs"] = now()

        async def receive():
            while True:
                message = await ws.receive()
                if message.type != WSMsgType.TEXT:
                    raise RuntimeError("Socket closed while awaiting results")
                event = json.loads(message.data)
                kind = event.get("type")
                if kind == "state":
                    continue
                at = now()
                if "audio" in event:
                    event["audioBase64Length"] = len(event.pop("audio"))
                with (output / (sid + "-events.jsonl")).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"atMs": at, **event}, ensure_ascii=False) + "\n")
                if kind in {"error", "provider_unavailable"}:
                    result["errors"].append(event)
                    raise RuntimeError(json.dumps(event, ensure_ascii=False)[:500])
                tid = event.get("turnId") or (event.get("origin") or {}).get("turnId")
                row = rows.get(tid)
                if row is None:
                    continue
                elapsed = round(at - row["sentAtMs"], 1)
                if kind == "turn_receipt":
                    row.setdefault("receiptMs", elapsed)
                elif kind == "audio":
                    row.setdefault("firstAudioMs", elapsed)
                    row["audioPackets"] = row.get("audioPackets", 0) + 1
                elif kind == "assistant_transcript":
                    row.setdefault("firstTextMs", elapsed)
                    row["transcript"] = event["text"] if event.get("done") else row.get("transcript", "") + event.get("text", "")
                elif kind == "response_status" and event.get("status") == "completed":
                    row["responseCompletedMs"] = elapsed

        reader = asyncio.create_task(receive())
        for index, (question, expected) in enumerate(CASES):
            if index and interval_ms:
                await asyncio.sleep(interval_ms / 1000)
            tid = uuid4().hex
            row = {"turnId": tid, "question": question, "expected": expected, "sentAtMs": now()}
            rows[tid] = row
            result["turns"].append(row)
            await ws.send_json({"type": "control", "clientRequestId": tid, "transcript": question})
        result["actualGapsMs"] = [round(b["sentAtMs"] - a["sentAtMs"], 1)
                                  for a, b in zip(result["turns"], result["turns"][1:])]
        async with asyncio.timeout(args.completion_budget_ms / 1000):
            while True:
                if reader.done():
                    reader.result()
                state = await (await client.get("/api/state", params={"session": sid})).json()
                inbox = {t["turnId"]: t for t in state["workLedger"]["turnInbox"]}
                for tid, row in rows.items():
                    answer = inbox.get(tid, {}).get("answer") or {}
                    row["turnStatus"] = inbox.get(tid, {}).get("status")
                    row["answerStatus"] = answer.get("status")
                    if answer.get("status") in {"failed", "cancelled"}:
                        raise AssertionError("Answer failed or cancelled: " + tid)
                    if answer.get("status") == "completed":
                        row.setdefault("durableCompletedMs", round(now() - row["sentAtMs"], 1))
                        row["answer"] = answer.get("text", "")
                        row["answerChannel"] = answer.get("channel")
                        row["answerModel"] = answer.get("model")
                        row["providerRequestId"] = answer.get("providerRequestId")
                if all(r.get("durableCompletedMs") and "receiptMs" in r for r in rows.values()) and result["turns"][-1].get("responseCompletedMs"):
                    break
                await asyncio.sleep(.1)
        records = journal(output, sid)
        decisions = {r["turnId"]: r["decision"] for r in records if r.get("type") == "control_decision"}
        generated = {r.get("payload", {}).get("origin", {}).get("turnId") for r in records
                     if r.get("eventType") == "response.finished"
                     and r.get("payload", {}).get("status") == "response.done"
                     and r.get("payload", {}).get("usage")}
        result["decisions"] = decisions
        for tid, row in rows.items():
            decision = decisions.get(tid, {})
            row["routingMs"] = decision.get("latencyMs")
            row["modelsUsed"] = decision.get("modelsUsed")
            row["checks"] = {
                "correctAnswer": re.sub(r"[\s。.!！]", "", row.get("answer", ""))
                    in {row["expected"], CHINESE_ANSWERS[row["expected"]]},
                "realProviderAnswer": bool(row.get("providerRequestId")) or tid in generated,
                "classified": decision.get("status") == "classified",
                "expectedRouter": decision.get("routerBackend") == "openrouter-decisions",
                "receiptBudget": row.get("receiptMs", float("inf")) <= args.receipt_budget_ms,
                "completionBudget": row.get("durableCompletedMs", float("inf")) <= args.completion_budget_ms,
            }
        last = result["turns"][-1]
        last["checks"]["latestAudioBudget"] = last.get("firstAudioMs", float("inf")) <= args.first_audio_budget_ms
        await ws.close()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        restored = await (await client.get("/api/state", params={"session": sid})).json()
        restored_answers = {t["turnId"]: t.get("answer") or {} for t in restored["workLedger"]["turnInbox"]}
        for tid, row in rows.items():
            row["checks"]["survivesDisconnect"] = restored_answers.get(tid, {}).get("text") == row.get("answer")
            row["passed"] = all(row["checks"].values())
        result["passed"] = all(r["passed"] for r in rows.values())
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:1000]
    finally:
        if reader:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if ws:
            await ws.close()
        result["elapsedMs"] = now()
    return result


async def verify(args):
    load_env(str(ROOT / ".env"))
    args.output.mkdir(parents=True, exist_ok=False)
    paths = [Path(__file__), ROOT / "egoassist/realtime/decisions_router.py",
             ROOT / "egoassist/realtime/modular.py", ROOT / "egoassist/application/composition.py",
             ROOT / "egoassist/realtime/turn_coordinator.py", ROOT / "egoassist/application/realtime_connection.py",
             *sorted((ROOT / "egoassist/prompts").glob("*.json"))]
    report = {"passed": False, "mock": False, "hardwareMeasured": False,
              "scope": "Three independent questions per fresh session, at 0/200/500 ms send gaps; real routing, generation, TTS, persistence.",
              "routerBackend": os.getenv("EGOASSIST_TURN_ROUTER_BACKEND", "qwen"),
              "realtimeBackend": os.getenv("EGOASSIST_REALTIME_BACKEND", "omni"),
              "latencySettings": {k: os.getenv(k, v) for k, v in {
                  "EGOASSIST_DECISIONS_CONNECTION_POOL": "1", "EGOASSIST_DECISIONS_PRECONNECT": "4",
                  "EGOASSIST_TTS_PREFETCH": "1"}.items()},
              "budgetsMs": {"receipt": args.receipt_budget_ms, "latestFirstAudio": args.first_audio_budget_ms,
                            "eachDurableAnswer": args.completion_budget_ms},
              "questions": CASES, "intervalsMs": [0, 200, 500],
              "hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}, "cases": []}
    (args.output / "plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(args.output / "runtime.json"),
                         "qwen3.5-omni-flash-realtime", "Ethan")
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/auth/register", json={"username": "rapid-interaction", "password": uuid4().hex})
            assert response.status == 200
            for interval in report["intervalsMs"]:
                row = await run_case(client, args.output, interval, args)
                report["cases"].append(row)
                (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({k: v for k, v in row.items() if k != "decisions"}, ensure_ascii=True), flush=True)
        report["hashMismatches"] = [f for f, h in report["hashes"].items() if hashlib.sha256((ROOT / f).read_bytes()).hexdigest() != h]
        report["passed"] = all(c["passed"] for c in report["cases"]) and not report["hashMismatches"]
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)[:1000]
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"])}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt-budget-ms", type=float, default=1000)
    parser.add_argument("--first-audio-budget-ms", type=float, default=8000)
    parser.add_argument("--completion-budget-ms", type=float, default=30000)
    raise SystemExit(0 if asyncio.run(verify(parser.parse_args())) else 1)
