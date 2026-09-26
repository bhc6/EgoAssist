"""Real archive API/browser navigation; labelled delayed/failed reads, no model calls."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import secrets
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.diary.store import DiaryStore
from run_egoassist import create_app


async def verify(output,base_url=None,source_diary=None):
    output.mkdir(parents=True,exist_ok=False)
    load_env(str(ROOT/'.env'))
    report={'passed':False,'pageErrors':[],'checks':{},'modelCalls':False,
            'injectedConditions':['delayed detail response','single HTTP 503']}
    try:
        async with AsyncExitStack() as stack:
            if base_url:
                base=base_url.rstrip('/')
            else:
                server=await stack.enter_async_context(TestServer(create_app(str(BUILTIN_SKILLS_ROOT),None,
                    str(output/'runtime.json'),'unused','Ethan')))
                base=str(server.make_url('')).rstrip('/')
            pw=await stack.enter_async_context(async_playwright())
            browser=await pw.chromium.launch(channel='msedge',headless=True)
            try:
                context=await browser.new_context(viewport={'width':1440,'height':1000})
                response=await context.request.post(base+'/api/auth/register',data={
                    'username':'archive-'+secrets.token_hex(4),'password':secrets.token_urlsafe(24)})
                assert response.ok
                uid=(await (await context.request.get(base+'/api/auth/me')).json())['user']['id']
                assert (await context.request.get(base+'/api/diary')).ok
                if source_diary:
                    assert not base_url, 'Historical fixture copies require an isolated app'
                    store=DiaryStore(output/'users'/uid)
                    # Copy only terminal invalidated archive rows. Never copy auth,
                    # schedules, pending jobs or mutate the original database.
                    with sqlite3.connect(source_diary.resolve().as_uri()+'?mode=ro',uri=True) as src, store.db() as dst:
                        rows=src.execute("SELECT * FROM entries WHERE status='stale'").fetchall()
                        dst.executemany('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)',rows)
                    report['copiedStaleEntries']=len(rows)
                for day in ('2000-01-01','2000-01-02','2000-01-03'):
                    response=await context.request.post(base+'/api/diary',data={'kind':'daily','anchor':day,'timezone':'Asia/Shanghai'})
                    assert response.status==202
                    row=await response.json()
                    async with asyncio.timeout(15):
                        while row['status'] in {'queued','running'}:
                            await asyncio.sleep(.1)
                            row=await (await context.request.get(base+'/api/diary/'+row['id'])).json()
                    assert row['status']=='empty'
                page=await context.new_page()
                page.on('pageerror',lambda error:report['pageErrors'].append(str(error)))
                await page.goto(base+'/diary.html')
                await page.wait_for_function("document.querySelector('#reading .period') && document.querySelector('#reading').getAttribute('aria-busy')==='false'")
                ids=await page.locator('#archive [data-id]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.id)')
                slow,target=ids[1:3]
                entered=asyncio.Event()
                async def delayed(route):
                    response=await route.fetch()
                    entered.set()
                    await asyncio.sleep(1)
                    await route.fulfill(response=response)
                await page.route('**/api/diary/'+slow,delayed)
                await page.locator(f'[data-id="{slow}"]').click()
                await entered.wait()
                assert await page.locator(f'[data-id="{slow}"]').get_attribute('aria-current')=='true'
                assert '正在读取这份档案' in await page.locator('#reading').inner_text()
                await page.locator(f'[data-id="{target}"]').click()
                await page.wait_for_function("id=>new URL(location.href).searchParams.get('entry')===id && document.querySelector('#reading').getAttribute('aria-busy')==='false'",arg=target)
                await asyncio.sleep(1.1)
                assert await page.locator(f'[data-id="{target}"]').get_attribute('aria-current')=='true'
                assert target in page.url
                report['checks']['immediateSelectionAndLatestWins']=True
                await page.unroute('**/api/diary/'+slow,delayed)
                async def unavailable(route):
                    await route.fulfill(status=503,json={'error':'测试读取故障'})
                await page.route('**/api/diary/'+slow,unavailable)
                await page.locator(f'[data-id="{slow}"]').click()
                await page.wait_for_function("!document.querySelector('#retryLoad').hidden")
                assert '暂时无法打开' in await page.locator('#reading').inner_text()
                await page.unroute('**/api/diary/'+slow,unavailable)
                await page.locator('#retryLoad').click()
                await page.wait_for_function("id=>new URL(location.href).searchParams.get('entry')===id && document.querySelector('#reading').getAttribute('aria-busy')==='false'",arg=slow)
                report['checks']['readFailureAndRetry']=True
                for identifier in ids[3:]:
                    await page.locator(f'[data-id="{identifier}"]').click()
                    await page.wait_for_function("id=>new URL(location.href).searchParams.get('entry')===id",arg=identifier)
                    assert '正文已撤下' in await page.locator('#reading').inner_text()
                    assert await page.locator('[data-regenerate]').is_visible()
                report['checks']['staleEntriesReadable']=len(ids)-3
                if len(ids)>3:
                    old=ids[-1]
                    await page.locator('[data-regenerate]').click()
                    await page.wait_for_function("old=>new URL(location.href).searchParams.get('entry')!==old && document.querySelector('#reading').getAttribute('aria-busy')==='false'",arg=old)
                    assert await page.locator(f'[data-id="{old}"]').count()==1
                    assert await page.locator('#archive [data-id]').count()==len(ids)+1
                    report['checks']['regenerateCreatesNewVersion']=True
                await page.set_viewport_size({'width':390,'height':844})
                await page.locator(f'[data-id="{target}"]').click()
                await page.wait_for_function("id=>new URL(location.href).searchParams.get('entry')===id",arg=target)
                assert await page.locator('#reading').evaluate('(n)=>n.getBoundingClientRect().top')<200
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await page.screenshot(path=str(output/'archive-mobile.png'))
                await page.reload()
                await page.wait_for_function("id=>document.querySelector('[data-id=\"'+id+'\"]').getAttribute('aria-current')==='true'",arg=target)
                report['checks']['mobileAndReloadSelection']=True
                report['passed']=not report['pageErrors']
            finally:
                await browser.close()
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)[:1200]
    (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=True),flush=True)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--base-url')
    parser.add_argument('--source-diary',type=Path)
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output,args.base_url,args.source_diary)) else 1)
