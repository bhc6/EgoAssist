"""Exercise the production UI in installed Chrome/Edge with isolated data.

No mocked endpoints or media. This verifies UI/account/session flows, not hardware.
Requires playwright; does not download a browser.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from run_egoassist import create_app


async def verify(output):
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'pageErrors': [], 'failedRequests': [], 'viewports': [],
              'mock': False, 'hardwareMeasured': False}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / 'runtime.json'),
                     'qwen3.5-omni-flash-realtime', 'Ethan')
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
                page.on('requestfailed', lambda req: report['failedRequests'].append(req.url))
                await page.goto(str(server.make_url('/')))
                await page.locator('#registerMode').click()
                await page.locator('#authUsername').fill('browser-acceptance')
                await page.locator('#authPassword').fill('isolated-browser-account-123')
                await page.locator('#authSubmit').click()
                await page.locator('#authGate').wait_for(state='hidden')
                await page.locator('#liveWorkflow').wait_for(state='visible')
                page.once('dialog', lambda dialog: dialog.accept('browser-real-session'))
                await page.locator('#newSession').click()
                await page.wait_for_function("document.querySelector('#sessionSelect').selectedOptions[0]?.textContent.includes('browser-real-session')")
                report['sessionCreated'] = True
                for width in (1440, 390):
                    await page.set_viewport_size({'width': width, 'height': 1000})
                    await page.screenshot(path=str(output / f'ui-{width}.png'), full_page=True)
                    layout = await page.evaluate('''() => ({width: innerWidth,
                        scrollWidth: document.documentElement.scrollWidth,
                        workflowVisible: document.querySelector('#liveWorkflow').getBoundingClientRect().height > 0})''')
                    report['viewports'].append(layout)
                await page.locator('#logout').click()
                await page.locator('#authGate').wait_for(state='visible')
                await page.locator('#loginMode').click()
                await page.locator('#authUsername').fill('browser-acceptance')
                await page.locator('#authPassword').fill('isolated-browser-account-123')
                await page.locator('#authSubmit').click()
                await page.locator('#authGate').wait_for(state='hidden')
                await page.wait_for_function("document.querySelector('#sessionSelect').selectedOptions[0]?.textContent.includes('browser-real-session')")
                report['sessionRestoredAfterLogin'] = True
                report['passed'] = (not report['pageErrors'] and not report['failedRequests']
                                    and all(v['scrollWidth'] <= v['width'] and v['workflowVisible']
                                            for v in report['viewports']))
            finally:
                await browser.close()
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    (output / 'acceptance.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(verify(args.output)) else 1)
