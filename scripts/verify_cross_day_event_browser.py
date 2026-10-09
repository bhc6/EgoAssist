"""Verify event phase citations in the real console, using an isolated fixture account."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from run_egoassist import create_app


async def verify(output):
    report = {'passed': False, 'errors': []}
    original = SessionJournal(output/'runtime/users/demo/sessions/cross-day.jsonl')
    runtime = output/('browser-runtime-' + uuid4().hex[:8])
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(runtime/'runtime.json'), 'unused', 'Ethan')
    async with TestServer(app) as server, async_playwright() as pw:
        browser = await pw.chromium.launch(channel='msedge', headless=True)
        try:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: report['errors'].append(str(error)))
            base = str(server.make_url('/'))
            response = await page.request.post(base+'api/auth/register', data={'username': 'cross-day-browser', 'password': 'Local-fixture-20261001'})
            assert response.status == 200
            uid = (await response.json())['user']['id']
            response = await page.request.post(base+'api/sessions')
            sid = (await response.json())['session']['id']
            journal = SessionJournal(runtime/'users'/uid/'sessions'/(sid+'.jsonl'), session_id=sid)
            # Preserve source IDs and timestamps from the actual model run in a
            # separate test account. The original journal is never rewritten.
            for row in original.entries():
                journal.append(row['type'], row)
            await page.goto(base)
            await page.locator('#authGate').wait_for(state='hidden')
            await page.wait_for_function('state?.sharedScene?.longEvents?.events?.length > 0')
            await page.locator('#longEventTitle').click()
            phases = page.locator('#longEvents details[data-disclosure*="-phase-"]')
            assert await phases.count() >= 4
            phase = phases.nth(0)
            await phase.locator('summary').click()
            await phase.locator('button[data-source-ids]').click()
            await phase.locator('blockquote').first.wait_for()
            assert '9月25日' in await phase.inner_text()
            event = await page.evaluate('state.sharedScene.longEvents.events[0]')
            refs = event['phases'][0]['sourceIds']
            evidence = await page.request.get(base+'api/scene/events/evidence', params={
                'session': sid, 'eventId': event['eventId'], 'sourceId': refs[0]})
            assert evidence.status == 200
            assert (await evidence.json())['total'] == 1
            invalid = await page.request.get(base+'api/scene/events/evidence', params={
                'session': sid, 'eventId': event['eventId'], 'sourceId': 'not-owned'})
            assert invalid.status == 400
            await page.locator('#longEvents').screenshot(path=str(output/'event-console.png'))
            report.update(passed=not report['errors'], phaseCount=await phases.count(),
                          phaseEvidenceClickable=True, invalidSourceRejected=True)
            # Also verify the standalone report's citation anchors.
            await page.goto((output/'index.html').resolve().as_uri())
            broken = await page.evaluate('''() => [...document.querySelectorAll('a[href^="#s-"]')]
                .filter(a => !document.getElementById(a.hash.slice(1))).length''')
            assert broken == 0
            await page.screenshot(path=str(output/'report-preview.png'))
            report['reportCitationAnchorsValid'] = True
        finally:
            await page.screenshot(path=str(output/'browser-last.png'))
            report['lastPageText'] = (await page.locator('body').inner_text())[:2000] if not report['passed'] else None
            await browser.close()
            (output/'browser-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.output.resolve()))
