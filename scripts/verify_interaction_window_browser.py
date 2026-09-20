"""Inspect persisted interaction-window evidence at desktop and mobile sizes."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from run_egoassist import create_app


async def verify(runtime, output):
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'pageErrors': [], 'viewports': []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(runtime), 'qwen3.5-omni-flash-realtime', 'Ethan')
    async with TestServer(app) as server, async_playwright() as pw:
        browser = await pw.chromium.launch(channel='msedge', headless=True)
        try:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
            await page.goto(str(server.make_url('/')))
            await page.locator('#authUsername').fill('interaction-check')
            await page.locator('#authPassword').fill('interaction-av-check-123')
            await page.locator('#authSubmit').click()
            await page.locator('#authGate').wait_for(state='hidden')
            await page.wait_for_function("document.querySelectorAll('.interaction-window').length > 0")
            assert await page.locator('#videoFps').input_value() == '4'
            for width in (1440, 390):
                await page.set_viewport_size({'width': width, 'height': 1000})
                details = page.locator('.interaction-window').first
                await details.evaluate('(el) => el.open = true')
                await details.scroll_into_view_if_needed()
                await page.wait_for_function("[...document.querySelectorAll('.interaction-window[open] img')].every(i => i.complete && i.naturalWidth > 0)")
                assert await details.locator('img').count() == 2
                overflow = await page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                assert not overflow, 'page horizontal overflow'
                await page.screenshot(path=str(output / f'window-{width}.png'))
                report['viewports'].append({'width': width, 'imagesLoaded': True, 'pageOverflow': overflow})
            assert not report['pageErrors']
            report['passed'] = True
        finally:
            await browser.close()
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser();parser.add_argument('--runtime', type=Path, required=True);parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args();asyncio.run(verify(args.runtime,args.output))
