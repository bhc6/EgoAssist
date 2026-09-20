"""Exercise the actual text composer with real providers and no input media."""
import argparse
import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(output):
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "realModels": True, "hardwareMeasured": False,
              "pageErrors": [], "sentTypes": [], "turns": []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                     "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                await page.add_init_script("""window.mediaRequestCount = 0;
                    const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                    navigator.mediaDevices.getUserMedia = (...args) => {
                        window.mediaRequestCount++; return getMedia(...args);
                    };""")
                page.on("pageerror", lambda error: report["pageErrors"].append(str(error)))

                def sent(payload):
                    try:
                        report["sentTypes"].append(json.loads(payload).get("type"))
                    except (ValueError, TypeError):
                        pass

                page.on("websocket", lambda ws: ws.on("framesent", sent))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("text-only-browser")
                await page.locator("#authPassword").fill("isolated-browser-test-123")
                await page.locator("#authSubmit").click()
                await page.locator("#authGate").wait_for(state="hidden")
                for text, expected in [("二加三等于多少？简短回答。", ["5", "五"]),
                                       ("把刚才的结果乘以四呢？简短回答。", ["20", "二十"])]:
                    await page.locator("#textTurnInput").fill(text)
                    await page.locator("#textTurnSend").click()
                    await page.wait_for_function("!document.querySelector('#textTurnInput').value && currentTurnId", timeout=60000)
                    tid = await page.evaluate("currentTurnId")
                    await page.wait_for_function("id => state?.workLedger?.turnInbox.some(t => t.turnId === id && t.answer?.status === 'completed' && t.answer?.text)", arg=tid, timeout=60000)
                    turn = await page.evaluate("id => state.workLedger.turnInbox.find(t => t.turnId === id)", tid)
                    assert any(value in turn["answer"]["text"] for value in expected)
                    assert turn["answer"]["inputToFirstAudioMs"] > 0
                    assert turn["answer"]["model"] != "qwen3.5-omni-flash-realtime"
                    report["turns"].append({"question": text, "turnId": tid, "answer": turn["answer"]})
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000 if width == 1440 else 844})
                    await page.screenshot(path=str(output / f"browser-{width}.png"), full_page=True)
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                report["mediaRequests"] = await page.evaluate("mediaRequestCount")
                assert report["mediaRequests"] == 0
                assert not set(report["sentTypes"]) & {"audio", "video"}
                assert not report["pageErrors"]
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_env()
    asyncio.run(verify(args.output))
