"""Real browser playback interruption using actual Qwen ASR over TTS audio."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from verify_real_voice_stop import synthesize


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "mock": False, "hardwareMeasured": False, "pageErrors": [], "viewports": []}
    try:
        pcm, report["fixture"] = await synthesize("让我安静一会儿，先别出声。", output, 0)
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                         "qwen3.5-omni-flash-realtime", "Ethan")
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: report["pageErrors"].append(str(error)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("voice-stop-browser")
                await page.locator("#authPassword").fill("isolated-voice-stop-browser-123")
                await page.locator("#authSubmit").click()
                await page.locator("#authGate").wait_for(state="hidden")
                await page.locator("#textTurnInput").fill("请详细解释乘法原理，至少讲二十句话。")
                await page.locator("#textTurnSend").click()
                await page.wait_for_function("playbackSources.size > 0 && Boolean(captionResponseId)", timeout=45000)
                await page.evaluate("""() => {
                    window.stopEvidence = {oldResponse: captionResponseId, started: performance.now(), events: []};
                    socket.addEventListener('message', event => {
                        const message = JSON.parse(event.data);
                        if (['playback_stop', 'speech_control', 'user_transcript'].includes(message.type)) {
                            stopEvidence.events.push({type: message.type, trigger: message.trigger,
                                atMs: performance.now() - stopEvidence.started, playingSources: playbackSources.size,
                                responseId: captionResponseId});
                        }
                    });
                }""")
                data = pcm + bytes(32000)
                for offset in range(0, len(data), 3200):
                    await page.evaluate("audio => socket.send(JSON.stringify({type:'audio', audio}))",
                                        base64.b64encode(data[offset:offset + 3200]).decode())
                    await asyncio.sleep(0.1)
                await page.wait_for_function("stopEvidence.events.some(e => e.type === 'speech_control')", timeout=30000)
                report["playback"] = await page.evaluate("stopEvidence")
                onset = next(e for e in report["playback"]["events"] if e.get("trigger") == "provider-speech-started")
                assert onset["playingSources"] == 0, onset
                assert await page.evaluate("playbackSources.size") == 0
                assert "已停止播报" in await page.locator("#duplexState").text_content()
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.screenshot(path=str(output / f"viewport-{width}.png"))
                    layout = await page.evaluate("({width:innerWidth, scrollWidth:document.documentElement.scrollWidth})")
                    assert layout["scrollWidth"] <= layout["width"], layout
                    report["viewports"].append(layout)
                await page.locator("#textTurnInput").fill("请只回答九乘九的结果。")
                await page.locator("#textTurnSend").click()
                await page.wait_for_function("playbackSources.size > 0 && captionResponseId !== stopEvidence.oldResponse", timeout=45000)
                report["newQuestionPlays"] = True
                report["passed"] = not report["pageErrors"]
            finally:
                await browser.close()
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
