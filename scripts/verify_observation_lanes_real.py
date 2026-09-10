"""Real Qwen task, ambient observation, dialogue and memory integration."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from real_session_support import journal
from verify_shared_scene import decode


async def verify(output, burst=False):
    load_env(str(ROOT / ".env"))
    os.environ["EGOASSIST_AMBIENT_WATCHLIST"] = ""
    os.environ["EGOASSIST_AMBIENT_INTERVAL_SECONDS"] = "2"
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "providerMock": False, "hardwareMeasured": False, "errors": []}
    source = ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4"
    _, image = decode(source)
    report["source"] = str(source)
    report["frameSha256"] = hashlib.sha256(image).hexdigest()
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                     "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("lanes-real")
                await page.locator("#authPassword").fill("isolated-lanes-123")
                await page.locator("#authSubmit").click()
                await page.wait_for_function("state?.sessionId")
                sid = await page.evaluate("state.sessionId")
                report["sessionId"] = sid
                await page.locator("#ambientGrant").click()
                await page.evaluate("async () => { await connectRealtime({media:false}); }")
                await page.evaluate("""() => socket.send(JSON.stringify({type:'control',
                    transcript:'我要做番茄炒蛋，请启动做菜任务，并详细介绍准备工作和操作步骤。', clientRequestId:'cooking-lanes'}))""")
                await page.wait_for_function("state?.task", timeout=45000)
                started = time.perf_counter()
                await page.evaluate("image => socket.send(JSON.stringify({type:'video', image}))", base64.b64encode(image).decode())
                await page.wait_for_function("state?.ambient?.observation?.media", timeout=35000)
                report["observationWallMs"] = round((time.perf_counter() - started) * 1000, 1)
                report["observation"] = await page.evaluate("state.ambient.observation")
                assert report["observation"]["media"]["sha256"] == report["frameSha256"]
                assert await page.evaluate("!!state.task")
                report["runtimeLanes"] = await page.evaluate("state.runtimeLanes")
                assert report["runtimeLanes"]["schemaVersion"] == "three-lanes.v1"
                assert report["runtimeLanes"]["observation"]["latestEvidenceId"]
                assert "memoryWork" not in await page.evaluate("state.ambient")
                await page.wait_for_function("state?.episodicMemory?.timelineViews?.case?.recent?.some(c => c.kind === 'observation')", timeout=15000)
                report["memory"] = await page.evaluate("state.episodicMemory.timelineViews.case.recent")
                await page.evaluate("""() => socket.send(JSON.stringify({type:'control',
                    transcript:'刚才的画面中有什么？请根据实际观察简短回答。', clientRequestId:'scene-lanes'}))""")
                await page.wait_for_function("state.workLedger.turnInbox.some(t => t.turnId === 'scene-lanes' && t.answer?.status === 'completed')", timeout=65000)
                report["answer"] = await page.evaluate("state.workLedger.turnInbox.find(t => t.turnId === 'scene-lanes')")
                rows = journal(output, sid)
                report["requests"] = [r for r in rows if r.get("eventType") == "perception.response.requested"]
                report["finished"] = [r for r in rows if r.get("eventType") == "perception.response.finished"]
                assert {r["payload"].get("lane") for r in report["requests"]} >= {"ambient", "perception"}
                assert all(r["payload"].get("providerStatus") == "completed" for r in report["finished"])
                report["rejections"] = [r for r in rows if r.get("eventType") in
                    {"perception.observation.rejected", "ambient.observation.rejected"}]
                assert not any(r.get("payload", {}).get("reason") in
                    {"malformed-provider-output", "invalid-provider-schema", "async-result-handler-failed"}
                    for r in report["rejections"]), report["rejections"]
                scene = next(r for r in rows if r.get("type") == "scene.turn" and r.get("turnId") == "scene-lanes")
                assert scene["observationContext"]["sourceId"]
                report["questionEvidence"] = scene["observationContext"]
                ambient_request = next(r for r in report["requests"] if r["payload"]["lane"] == "ambient")
                task_request = next(r for r in report["requests"] if r["payload"]["lane"] == "perception")
                ambient_done = next(r for r in report["finished"] if r["payload"]["lane"] == "ambient")
                task_done = next(r for r in report["finished"] if r["payload"]["lane"] == "perception")
                report["observersOverlapped"] = max(ambient_request["timestamp"], task_request["timestamp"]) < min(ambient_done["timestamp"], task_done["timestamp"])
                assert report["observersOverlapped"]
                dialogue_start = next(r for r in rows if r.get("eventType") == "response.created"
                    and r.get("payload", {}).get("origin", {}).get("turnId") == "cooking-lanes")
                dialogue_end = next(r for r in rows if r.get("eventType") == "response.finished"
                    and r.get("payload", {}).get("origin", {}).get("turnId") == "cooking-lanes")
                report["dialogueOverlappedObservation"] = max(ambient_request["timestamp"], dialogue_start["timestamp"]) < min(ambient_done["timestamp"], dialogue_end["timestamp"])
                assert report["dialogueOverlappedObservation"]
                if burst:
                    await page.evaluate("""image => {
                        window.burstAudit = {started:performance.now(), events:[]};
                        socket.addEventListener('message', event => {
                            const row = JSON.parse(event.data);
                            if (['turn_receipt', 'interaction_status'].includes(row.type))
                                burstAudit.events.push({...row, atMs:performance.now()-burstAudit.started});
                        });
                        socket.send(JSON.stringify({type:'video', image}));
                        for (const [id, text] of [
                            ['burst-one', '画面上桌布是什么颜色？请用一句话回答。'],
                            ['burst-two', '另一个独立问题：画面中能看到白板吗？请用一句话回答。']])
                            socket.send(JSON.stringify({type:'control', transcript:text, clientRequestId:id}));
                    }""", base64.b64encode(image).decode())
                    await page.wait_for_function("['burst-one','burst-two'].every(id => state.workLedger.turnInbox.some(t => t.turnId === id && t.answer?.status === 'completed'))", timeout=65000)
                    report["burst"] = await page.evaluate("({events:burstAudit.events, turns:state.workLedger.turnInbox.filter(t=>t.turnId.startsWith('burst-'))})")
                    receipts = [e for e in report["burst"]["events"] if e["type"] == "turn_receipt"]
                    assert {e["turnId"] for e in receipts} == {"burst-one", "burst-two"}
                    assert max(e["atMs"] for e in receipts) < 2000, receipts
                    rows = journal(output, sid)
                    requests = [r for r in rows if r.get("type") == "scene.question.request" and r.get("turnId", "").startswith("burst-")]
                    results = [r for r in rows if r.get("type") == "scene.question.result" and r.get("turnId", "").startswith("burst-")]
                    assert len(requests) == len(results) == 2
                    assert all(r["status"] == "observed" and r["requestId"] for r in results), results
                    report["burst"]["visionOverlapped"] = max(r["timestamp"] for r in requests) < min(r["timestamp"] for r in results)
                    assert report["burst"]["visionOverlapped"]
                    assert all(r["frame"]["sha256"] == report["frameSha256"] for r in results)
                    report["burst"]["visualResults"] = results
                await page.reload()
                await page.wait_for_function("state?.ambient?.observation?.media")
                report["restoredObservation"] = await page.evaluate("state.ambient.observation")
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.screenshot(path=str(output / f"lanes-{width}.png"), full_page=True)
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    assert await page.locator("#executionLanes > div").count() == 3
                assert not report["errors"]
            finally:
                await browser.close()
        report["passed"] = True
    except Exception as exc:
        import traceback
        report["error"] = type(exc).__name__ + ": " + str(exc)
        report["traceback"] = traceback.format_exc()
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({k: report.get(k) for k in ("passed", "error", "observationWallMs", "observersOverlapped")}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--burst", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output, args.burst)) else 1)
