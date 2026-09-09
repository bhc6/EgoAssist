"""Real providers, actual EgoLife media through app WebSocket, and browser UI checks.

The camera stream is a prerecorded image fixture, not physical glasses.
History overflow uses explicitly labelled UI fixtures, not fabricated model results.
"""
import argparse
import asyncio
import base64
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
from verify_shared_scene import decode


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "providerMock": False, "hardwareMeasured": False, "errors": []}
    pcm, image = decode(ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4")
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("shared-scene-real")
                await page.locator("#authPassword").fill("isolated-real-scene-123")
                await page.locator("#authSubmit").click()
                await page.locator("#authGate").wait_for(state="hidden")

                async def question(text):
                    started = time.perf_counter()
                    await page.locator("#textTurnInput").fill(text)
                    await page.locator("#textTurnSend").click()
                    await page.wait_for_function("!document.querySelector('#textTurnInput').value && currentTurnId", timeout=40000)
                    tid = await page.evaluate("currentTurnId")
                    await page.wait_for_function("id => state?.workLedger?.turnInbox.some(t => t.turnId === id && t.answer?.status === 'completed' && t.answer?.text)", arg=tid, timeout=60000)
                    turn = await page.evaluate("id => state.workLedger.turnInbox.find(t => t.turnId === id)", tid)
                    assert turn["answer"]["inputToFirstAudioMs"] > 0
                    assert turn["answer"]["inputToCompleteMs"] >= turn["answer"]["inputToFirstAudioMs"]
                    return {"turn": turn, "wallMs": round((time.perf_counter() - started) * 1000, 1)}

                report["textFirst"] = await question("七乘八是多少？只回答数字。")
                assert "56" in report["textFirst"]["turn"]["answer"]["text"]
                # Exercise enabling devices after a text-only WebSocket is already ready.
                await page.evaluate("""async image => {
                    const picture = new Image(); picture.src = 'data:image/jpeg;base64,' + image; await picture.decode();
                    const canvas = document.createElement('canvas'); canvas.width = 768; canvas.height = 432;
                    canvas.getContext('2d').drawImage(picture, 0, 0);
                    const camera = canvas.captureStream(2);
                    window.fixtureCanvas = canvas;
                    const sound = new AudioContext(); await sound.resume();
                    const target = sound.createMediaStreamDestination();
                    const oscillator = sound.createOscillator(), gain = sound.createGain(); gain.gain.value = 0;
                    oscillator.connect(gain).connect(target); oscillator.start();
                    window.fixtureSound = sound;
                    navigator.mediaDevices.getUserMedia = async () => new MediaStream([...camera.getVideoTracks(), ...target.stream.getAudioTracks()]);
                }""", base64.b64encode(image).decode())
                await page.locator("#cameraButton").click()
                await page.wait_for_function("state?.sharedScene?.camera === 'receiving'", timeout=20000)
                report["textThenCamera"] = True
                report["visualQuestion"] = await question("请描述摄像头中桌布的颜色和纹理，不要猜测姓名。")
                reply = report["visualQuestion"]["turn"]["answer"]["text"]
                assert "格" in reply and any(color in reply for color in ("红", "橙", "粉")), reply
                # Stop fixture silence, then inject real dataset PCM at wall-clock rate.
                await page.evaluate("stopAudioCapture()")
                before = await page.evaluate("(state.sharedScene?.utterances || []).map(t => t.turnId)")
                data = pcm + bytes(32000)
                for offset in range(0, len(data), 3200):
                    await page.evaluate("audio => socket.send(JSON.stringify({type:'audio', audio}))", base64.b64encode(data[offset:offset + 3200]).decode())
                    await asyncio.sleep(0.1)
                await page.wait_for_function("before => state?.sharedScene?.utterances.some(t => !before.includes(t.turnId) && t.speaker?.status !== 'pending' && t.speaker?.status !== 'account-input')", arg=before, timeout=35000)
                report["realMicrophoneTurns"] = await page.evaluate("before => state.sharedScene.utterances.filter(t => !before.includes(t.turnId))", before)
                assert any(t.get("audio") and t.get("synchronizedFrames") for t in report["realMicrophoneTurns"]), report["realMicrophoneTurns"]
                assert all(not t["speaker"].get("wearer") for t in report["realMicrophoneTurns"])
                await page.evaluate("stopMedia()")
                await page.reload()
                await page.locator("#authGate").wait_for(state="hidden")
                await page.wait_for_function("state?.workLedger?.turnInbox.length >= 2")
                report["restoredTurns"] = await page.evaluate("state.workLedger.turnInbox.length")
                report["performance"] = await page.evaluate("state.performance")
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.evaluate("scrollTo(0,0)")
                    await page.screenshot(path=str(output / f"viewport-{width}.png"))
                    await page.screenshot(path=str(output / f"full-{width}.png"), full_page=True)
                    bounds = await page.evaluate("""() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
                        inputBelowVideo: document.querySelector('#textTurnForm').getBoundingClientRect().top >= document.querySelector('.camera').getBoundingClientRect().bottom,
                        historyScrollable: getComputedStyle(document.querySelector('#turnHistory')).overflowY === 'auto'})""")
                    report.setdefault("viewports", []).append(bounds)
                    assert bounds["scrollWidth"] <= width and bounds["inputBelowVideo"] and bounds["historyScrollable"], bounds
                # Isolated rendering test: deliberately labelled UI fixture, no model claim.
                report["historyUIFixture"] = await page.evaluate("""() => {
                    const realState = state;
                    state = {...state, workLedger: {...state.workLedger, turnInbox: Array.from({length:25}, (_, i) => ({turnId:'ui-fixture-'+i, text:'UI layout fixture '+i, status:'completed'}))}};
                    renderWorkCenter(); const history = document.querySelector('#turnHistory'); history.scrollTop = 100;
                    renderWorkCenter(); const result = {count:history.children.length, preservedScroll:history.scrollTop === 100, overflow:history.scrollHeight > history.clientHeight};
                    state = realState; renderWorkCenter(); return result;
                }""")
                assert report["historyUIFixture"] == {"count": 25, "preservedScroll": True, "overflow": True}
                assert not report["errors"], report["errors"]
                report["passed"] = True
            finally:
                await browser.close()
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
