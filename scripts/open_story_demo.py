"""Open a persistent real browser for the presenter, with sensors initially off."""
import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def open_demo(args):
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(args.profile.resolve()), channel="msedge", headless=False,
            viewport={"width": 1440, "height": 1000})
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(args.base_url)
        if await page.locator("#authGate").is_visible():
            await page.locator("#authUsername").fill("story-demo-r4")
            await page.locator("#authPassword").fill("EgoAssist-demo-2026")
            await page.locator("#authSubmit").click()
            await page.locator("#authGate").wait_for(state="hidden")
        await page.wait_for_function("state?.workLedger")
        await page.locator("#microphoneEnabled").uncheck()
        page.once("dialog", lambda dialog: dialog.accept("现场演示 · 电脑摄像头与麦克风"))
        await page.locator("#newSession").click()
        await page.wait_for_function("state?.workLedger && !sessionSwitchBusy")
        await page.locator("#textTurnInput").fill("我的备用钥匙现在放在哪里？")
        exhibit = await context.new_page()
        await exhibit.goto(args.presentation.resolve().as_uri())
        await exhibit.bring_to_front()
        args.ready.write_text("ready", encoding="ascii")
        closed = asyncio.Event()
        context.on("close", lambda *args: closed.set())
        await closed.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--presentation", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8770")
    asyncio.run(open_demo(parser.parse_args()))
