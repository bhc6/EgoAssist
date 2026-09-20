"""Check the final local presentation and real console microphone control in Edge."""
import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def verify(args):
    report = {"passed": False, "pageErrors": [], "viewports": [], "media": []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        try:
            page = await browser.new_page()
            page.on("pageerror", lambda error: report["pageErrors"].append(str(error)))
            await page.goto(args.presentation.resolve().as_uri())
            for width, height in ((1440, 1000), (390, 844)):
                await page.set_viewport_size({"width": width, "height": height})
                for image in await page.locator("img").all():
                    await image.scroll_into_view_if_needed()
                    await image.evaluate("node=>node.decode()")
                await page.evaluate("scrollTo(0,0)")
                await page.screenshot(path=str(args.presentation.parent / f"presentation-{width}.png"))
                layout = await page.evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})")
                assert layout["scrollWidth"] <= width, layout
                report["viewports"].append(layout)
            for media in await page.locator("audio,video").all():
                state = await media.evaluate("""node=>new Promise((resolve,reject)=>{
                    node.preload='metadata';node.onloadedmetadata=()=>resolve({src:node.getAttribute('src'),duration:node.duration});
                    node.onerror=()=>reject(Error(node.error?.message));node.load();
                })""")
                assert state["duration"] > 0
                report["media"].append(state)
            video = page.locator("video").first
            await video.evaluate("node=>{node.muted=true;return node.play()}")
            await page.wait_for_function("document.querySelector('video').currentTime > 0.1")
            await video.evaluate("node=>node.pause()")
            report["recordingPlayback"] = True
            await page.goto(args.base_url)
            await page.locator("#authUsername").fill("story-demo-r4")
            await page.locator("#authPassword").fill("EgoAssist-demo-2026")
            await page.locator("#authSubmit").click()
            await page.locator("#authGate").wait_for(state="hidden")
            await page.locator("#microphoneEnabled").uncheck()
            await page.reload()
            await page.locator("#authGate").wait_for(state="hidden")
            assert not await page.locator("#microphoneEnabled").is_checked()
            report["microphoneChoiceSurvivesReload"] = True
            for width, height in ((1440, 1000), (390, 844)):
                await page.set_viewport_size({"width": width, "height": height})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            report["passed"] = not report["pageErrors"]
        except Exception as exc:
            report["error"] = type(exc).__name__ + ": " + str(exc)
        finally:
            await browser.close()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--presentation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8770")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args)) else 1)
