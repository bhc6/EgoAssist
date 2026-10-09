"""Real daily/weekly/monthly diary acceptance using fresh authored demo notes.

No imported private journals or historical output directories are required.
The notes cover one recorded day, not a measured week or month of life.
"""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import secrets
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.diary.generator import references
from run_egoassist import create_app


async def verify(output):
    load_env(str(ROOT/'.env'))
    output.mkdir(parents=True, exist_ok=False)
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    report = {'passed': False, 'realProviders': True, 'syntheticInputs': True,
              'sourceScope': 'Two authored notes on one recorded day; not a real week/month of life.',
              'anchor': day, 'periods': [], 'pageErrors': []}
    credentials = {'username': 'diary-periods-'+secrets.token_hex(3), 'password': secrets.token_urlsafe(24)}
    try:
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output/'runtime.json'), 'unused', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/api/auth/register', json=credentials)).status == 200
            for text in ('上午完成演示原型检查；下午要整理参考资料，现在还没完成。',
                         '仓库收到七箱配件，其中一箱外包装潮湿，内部是否损坏尚未检查。'):
                assert (await client.post('/api/diary/notes', json={'day': day, 'text': text})).status == 201
            for kind in ('daily', 'weekly', 'monthly'):
                response = await client.post('/api/diary', json={'kind': kind, 'anchor': day, 'timezone': 'Asia/Shanghai'})
                assert response.status == 202, await response.text()
                row = await response.json()
                async with asyncio.timeout(240):
                    while row['status'] in {'queued', 'running'}:
                        await asyncio.sleep(.5)
                        row = await (await client.get('/api/diary/'+row['id'])).json()
                (output/(kind+'.json')).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
                assert row['status'] == 'completed', row.get('error')
                assert row['calls'] and all(call.get('providerRequestId') for call in row['calls'])
                assert row['coverage']['records'] == 2 and row['coverage']['omittedRecords'] == 0
                source_ids = {source['sourceId'] for source in row['sources']}
                assert references(row['body']) and references(row['body']) <= source_ids
                exported = await client.get('/api/diary/'+row['id']+'/export')
                assert exported.status == 200
                (output/(kind+'.md')).write_text(await exported.text(), encoding='utf-8')
                report['periods'].append({'kind': kind, 'id': row['id'], 'status': row['status'],
                                          'sources': len(source_ids), 'providerCalls': len(row['calls'])})
                print(kind+': completed with source citations', flush=True)
            await client.post('/api/sessions')
            entries = (await (await client.get('/api/diary')).json())['entries']
            assert {r['id'] for r in report['periods']} <= {r['id'] for r in entries}
            report['crossSessionArchive'] = True
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel='msedge', headless=True)
                try:
                    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
                    await page.goto(str(client.make_url('/diary.html')))
                    await page.locator('#login').wait_for(state='visible')
                    await page.locator('[name=username]').fill(credentials['username'])
                    await page.locator('[name=password]').fill(credentials['password'])
                    await page.locator('#loginForm button').click()
                    await page.locator('#app').wait_for(state='visible')
                    for item in report['periods']:
                        await page.locator('[data-id="'+item['id']+'"]').click()
                        await page.wait_for_function('kind => document.querySelector("#reading .eyebrow")?.textContent === kind',
                            arg={'daily': '日记', 'weekly': '周回顾', 'monthly': '月回顾'}[item['kind']])
                        await page.locator('.claim details summary').first.click()
                        assert await page.locator('.claim .source').first.inner_text()
                        await page.screenshot(path=str(output/(item['kind']+'-desktop.png')), full_page=True)
                    await page.reload()
                    await page.wait_for_function('document.querySelectorAll("#reading .claim").length > 0')
                    await page.set_viewport_size({'width': 390, 'height': 844})
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await page.screenshot(path=str(output/'archive-mobile.png'), full_page=True)
                    assert not report['pageErrors'], report['pageErrors']
                finally:
                    await browser.close()
            report['passed'] = True
    except Exception as exc:
        report['error'] = type(exc).__name__+': '+str(exc)[:1600]
    (output/'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    raise SystemExit(0 if asyncio.run(verify(parser.parse_args().output.resolve())) else 1)
