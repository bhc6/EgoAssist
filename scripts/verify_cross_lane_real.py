"""Real Qwen observation-memory conflict, proactive question, correction and repeat control."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from real_session_support import journal
from verify_shared_scene import decode


async def verify(output):
    load_env(str(ROOT / ".env"))
    os.environ["EGOASSIST_AMBIENT_WATCHLIST"] = ""
    os.environ["EGOASSIST_AMBIENT_INTERVAL_SECONDS"] = "2"
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "providerMock": False, "hardwareMeasured": False, "errors": []}
    source = ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4"
    _, image = decode(source)
    report.update(source=str(source), frameSha256=hashlib.sha256(image).hexdigest())
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("cross-lane-real")
                await page.locator("#authPassword").fill("isolated-cross-lane-123")
                await page.locator("#authSubmit").click()
                await page.wait_for_function("state?.sessionId")
                sid = await page.evaluate("state.sessionId")
                report["sessionId"] = sid
                await page.locator("#ambientGrant").click()
                await page.locator("#textTurnInput").fill("请把这个事实记入长期记忆：我面前这张桌子的桌布是纯蓝色，没有图案。")
                await page.locator("#textTurnInput").press("Enter")
                await page.wait_for_function("state.episodicMemory.timelineViews.participant.recent.some(c=>c.text.includes('蓝色'))", timeout=45000)
                await page.wait_for_function("!state.runtimeLanes.interaction.scheduler.pending && !state.runtimeLanes.interaction.scheduler.activeResponseIds.length && playbackSources.size === 0", timeout=45000)
                report["initialMemory"] = await page.evaluate("state.episodicMemory.timelineViews.participant.recent")
                started = time.perf_counter()
                await page.evaluate("image=>socket.send(JSON.stringify({type:'video',image}))", base64.b64encode(image).decode())
                await page.wait_for_function("state.runtimeLanes.observation.reviews.some(r=>r.referenceCheck)", timeout=30000)
                report["comparisonWallMs"] = round((time.perf_counter()-started)*1000, 1)
                review = await page.evaluate("state.runtimeLanes.observation.reviews.find(r=>r.referenceCheck)")
                report["comparison"] = review
                assert review["referenceStatus"] == "source-bound-hypothesis", review
                assert review["referenceCheck"]["relation"] in {"conflict", "uncertain"}, review
                assert review["references"] and review["question"], review
                await page.wait_for_function("id=>state.runtimeLanes.observation.reviews.some(r=>r.candidateId===id && ['audio-sent','played'].includes(r.status))", arg=review["candidateId"], timeout=30000)
                report["questionDelivery"] = await page.evaluate("id=>state.runtimeLanes.observation.reviews.find(r=>r.candidateId===id)", review["candidateId"])
                assert report["questionDelivery"]["draftId"]
                await page.wait_for_function("playbackSources.size === 0", timeout=20000)
                await page.locator("#textTurnInput").fill("我刚才说错了。这张桌子的桌布是红白相间的格子，不是纯蓝色。请更正刚才那条记忆。")
                await page.locator("#textTurnInput").press("Enter")
                await page.wait_for_function("state.episodicMemory.timelineViews.participant.recent.some(c=>c.text.includes('红白'))", timeout=45000)
                await page.wait_for_function("id=>state.runtimeLanes.observation.reviews.some(r=>r.candidateId===id && r.status==='closed')", arg=review["candidateId"], timeout=15000)
                report["correctedMemory"] = await page.evaluate("state.episodicMemory.timelineViews.participant.recent")
                original_claims = {c["id"] for c in report["initialMemory"] if c["kind"] in {"fact", "state", "preference"}}
                if not original_claims:
                    original_claims = {c["id"] for c in report["initialMemory"] if c["kind"] == "event"}
                assert original_claims
                assert not any(c["id"] in original_claims and c["status"] == "active" for c in report["correctedMemory"]), report["correctedMemory"]
                assert any(c.get("supersedes") in original_claims for c in report["correctedMemory"])
                await page.reload()
                await page.wait_for_function("state?.runtimeLanes?.observation?.reviews?.length")
                await page.evaluate("async()=>{await connectRealtime({media:false});}")
                count = await page.evaluate("state.runtimeLanes.observation.reviews.length")
                await page.evaluate("image=>socket.send(JSON.stringify({type:'video',image}))", base64.b64encode(image).decode())
                await page.wait_for_function("n=>state.runtimeLanes.observation.reviews.length>n", arg=count, timeout=30000)
                report["afterRefresh"] = await page.evaluate("state.runtimeLanes.observation.reviews")
                latest = report["afterRefresh"][-1]
                assert latest["status"] not in {"ready", "audio-sent", "played"}, latest
                await page.locator("[data-disclosure='observation-reviews']").evaluate("node=>node.open=true")
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.screenshot(path=str(output / f"review-{width}.png"), full_page=True)
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
                rows = journal(output, sid)
                report["reviewEvents"] = [r for r in rows if r.get("type") == "observation.review"]
                report["providerRuns"] = [r for r in rows if r.get("eventType") == "perception.response.finished"]
                assert all(r["payload"].get("providerStatus") == "completed" for r in report["providerRuns"])
                assert len([r for r in report["reviewEvents"] if r["status"] == "audio-sent"]) == 1
                assert not report["errors"]
            finally:
                await browser.close()
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        report["traceback"] = traceback.format_exc()
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({k: report.get(k) for k in ("passed", "error", "comparisonWallMs")}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
