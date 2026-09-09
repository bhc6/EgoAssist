"""Real report requests over imported EgoLife text, with server shutdown/restart."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.realtime.context_harness import SessionJournal
from run_egoassist import create_app


async def verify(args):
    load_env(str(ROOT / ".env"))
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "mock": False, "hardwareMeasured": False,
              "source": str(args.source), "recordingTimeIsImportTime": True}
    credentials = {"username": "report-recovery", "password": "isolated-recovery-account-123"}

    def application():
        return create_app(str(BUILTIN_SKILLS_ROOT), None, str(args.output / "runtime.json"),
                          "qwen3.5-omni-flash-realtime", "Ethan")

    try:
        async with TestClient(TestServer(application())) as client:
            auth = await (await client.post("/api/auth/register", json=credentials)).json()
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            path = args.output / "users" / auth["user"]["id"] / "sessions" / (sid + ".jsonl")
            journal = SessionJournal(path, session_id=sid)
            dataset = json.loads(args.source.read_text(encoding="utf-8"))
            for source in dataset["records"]:
                journal.append_message("user", json.dumps(source, ensure_ascii=False), source="imported-egolife-annotation")
            jobs = []
            for kind in ("daily", "weekly", "monthly"):
                accepted = await client.post("/api/insights", json={"sessionId": sid, "kind": kind})
                assert accepted.status == 202
                jobs.append(await accepted.json())
            report["accepted"] = jobs
            cancelled = await client.post("/api/insights/cancel", json={"sessionId": sid, "jobId": jobs[1]["job"]["jobId"]})
            assert cancelled.status == 200
            async with asyncio.timeout(10):
                while not any(e["type"] == "insight.request" for e in journal.entries()):
                    await asyncio.sleep(0.02)
            report["beforeShutdown"] = (await (await client.get("/api/insights", params={"session": sid})).json())["jobs"]
            assert report["beforeShutdown"][0]["status"] == "running"
        print("server stopped with real provider request in flight", flush=True)
        async with TestClient(TestServer(application())) as client:
            assert (await client.post("/api/auth/login", json=credentials)).status == 200
            started = time.perf_counter()
            async with asyncio.timeout(100):
                while True:
                    payload = await (await client.get("/api/insights", params={"session": sid})).json()
                    if not any(j["status"] in {"queued", "running"} for j in payload["jobs"]):
                        break
                    await asyncio.sleep(0.2)
            report["afterRestart"] = payload["jobs"]
            report["recoveryMs"] = (time.perf_counter() - started) * 1000
            assert [j["status"] for j in payload["jobs"]] == ["completed", "cancelled", "completed"]
            report["results"] = []
            for job in payload["jobs"]:
                if job.get("resultEventId"):
                    result = await (await client.get("/api/insights", params={"session": sid, "result": job["resultEventId"]})).json()
                    result = result["results"][0]
                    assert result["coverage"]["includedRecords"] == len(dataset["records"])
                    assert result["providerRequestId"] and result["summary"]
                    report["results"].append(result)
            report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (args.output / "acceptance.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"results", "accepted"}}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args)) else 1)
