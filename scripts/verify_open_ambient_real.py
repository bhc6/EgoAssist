"""Real Qwen ambient observation of EgoLife frames through the browser app."""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import av
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(output):
    load_env(str(ROOT / ".env"))
    os.environ["EGOASSIST_AMBIENT_WATCHLIST"] = ""
    os.environ["EGOASSIST_AMBIENT_INTERVAL_SECONDS"] = "2"
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "providerMock": False, "hardwareMeasured": False, "observations": [], "errors": []}
    source = ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4"
    report["source"] = str(source)
    frames = []
    with av.open(str(source)) as container:
        for frame in container.decode(video=0):
            if frame.time is not None and frame.time >= (3, 10, 20)[len(frames)]:
                buffer = io.BytesIO()
                frame.to_image().resize((768, 432)).save(buffer, format="JPEG")
                frames.append(buffer.getvalue())
                if len(frames) == 3:
                    break
    assert len(frames) == 3
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width":1440, "height":1000})
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("ambient-real")
                await page.locator("#authPassword").fill("isolated-ambient-123")
                await page.locator("#authSubmit").click()
                await page.wait_for_function("state?.sessionId")
                await page.locator("#ambientGrant").click()
                await page.wait_for_function("ambientState?.consented")
                await page.evaluate("""async () => {
                    await connectRealtime({media:false});
                }""")
                for index, data in enumerate(frames):
                    before = await page.evaluate("state.ambient?.observation?.frameId")
                    started = time.perf_counter()
                    await page.evaluate("image => socket.send(JSON.stringify({type:'video', image}))", base64.b64encode(data).decode())
                    await page.wait_for_function("before => state?.ambient?.observation?.frameId && state.ambient.observation.frameId !== before", arg=before, timeout=25000)
                    observation = await page.evaluate("state.ambient.observation")
                    assert observation["summary"] and observation["media"]["sha256"] == hashlib.sha256(data).hexdigest()
                    observation["wallMs"] = round((time.perf_counter()-started)*1000, 1)
                    report["observations"].append(observation)
                    await asyncio.sleep(2.1)
                assert any(o["observed"] and o["activityId"] != "take-medication" for o in report["observations"])
                report["memory"] = await page.evaluate("state.episodicMemory.timelineViews.case.recent")
                assert any(c.get("kind") == "observation" for c in report["memory"]), report["memory"]
                await page.locator("#ambientSettings > summary").click()
                await page.locator("#ambientInterval").fill("7")
                await page.locator("#ambientQuestions").uncheck()
                await page.locator("#ambientSave").click()
                await page.wait_for_function("state.ambient.intervalSeconds === 7 && !state.ambient.questionsEnabled")
                await page.reload()
                await page.wait_for_function("state?.ambient?.observation?.media && state.ambient.intervalSeconds === 7")
                await page.wait_for_function("document.querySelector('#ambientInterval').value === '7'")
                assert not await page.evaluate("state.ambient.questionsEnabled")
                report["restored"] = True
                await page.locator("#textTurnInput").fill("刚才我没有说话时，画面里有什么？只说你观察记录里有的内容。")
                await page.locator("#textTurnSend").click()
                await page.wait_for_function("state.workLedger.turnInbox.some(t => t.answer?.status === 'completed')", timeout=60000)
                report["recall"] = await page.evaluate("state.workLedger.turnInbox.find(t => t.answer?.status === 'completed').answer")
                assert any(word in report["recall"]["text"] for word in ("桌", "手机", "笔", "房间")), report["recall"]
                from egoassist.memory.insights import InsightGenerator
                from egoassist.memory.temporal import ClockContext
                from egoassist.realtime.context_harness import SessionJournal
                paths = list((output / "users").rglob("sessions/*.jsonl"))
                report["dailyReport"] = await InsightGenerator(os.environ["QWEN_API_KEY"]).generate(
                    user_id="real-test", journal=SessionJournal(paths[0]), paths=paths,
                    kind="daily", anchor=None, clock=ClockContext.current("Asia/Shanghai"))
                assert report["dailyReport"]["status"] == "completed", report["dailyReport"]
                assert any(s["sourceKind"] == "visual-observation" for s in report["dailyReport"]["sources"])
                for width in (1440, 390):
                    await page.set_viewport_size({"width":width, "height":1000})
                    await page.screenshot(path=str(output / f"ambient-{width}.png"))
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert not report["errors"], report["errors"]
            finally:
                await browser.close()
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "error": report.get("error")}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
