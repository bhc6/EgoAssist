"""Actual diary provider/API/browser verification over archived demonstration records.

The imported records are previous real system runs, not a field study or a
month of personal activity. Original timestamps, IDs, failures and corrections
are preserved. No generated diary text is scripted.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.diary.sources import collect,period_for
from run_egoassist import create_app


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


async def main(out):
    load_env(str(ROOT/'.env'));out.mkdir(parents=True,exist_ok=False)
    runtime=out/'runtime';report={'passed':False,'mock':False,'fieldStudy':False,
        'sourceScope':'Imported prior real demonstration journals; one recorded day, not a real week/month of personal life.',
        'imports':[],'results':[]}
    credentials={'username':'diary-demo','password':'Diary-local-demo-20260917'}
    files=[ROOT/'outputs/natural-story-20260916-r4-elder/journals/elder.json',
           ROOT/'outputs/natural-story-20260916-r4-career-life/journals/career.json',
           ROOT/'outputs/egolife-history-browser-20260916-r2/journals/q102.json']
    try:
        async with TestClient(TestServer(create_app(str(BUILTIN_SKILLS_ROOT),None,str(runtime/'runtime.json'),'unused','Ethan'))) as client:
            auth=await (await client.post('/api/auth/register',json=credentials)).json();uid=auth['user']['id']
            session=(await (await client.post('/api/sessions')).json())['session']['id']
            ids=[]
            for path in files:
                rows=json.loads(path.read_text('utf-8')); header=next(r for r in rows if r['type']=='session');sid=header['id'];ids.append(sid)
                header.setdefault('metadata',{})['diaryProvenance']='导入的真实系统演示日志；使用脚本化问题，不是本账户真人生活。EgoLife 是重放素材，不能声称用户亲历。'
                target=runtime/'users'/uid/'sessions'/f'{sid}.jsonl'
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text('\n'.join(json.dumps(row,ensure_ascii=False) for row in rows)+'\n',encoding='utf-8')
                report['imports'].append({'file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'sessionId':sid})
            original_db=ROOT/'.egoassist-natural-demo-r4/timeline.sqlite3'
            with sqlite3.connect(original_db.resolve().as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(runtime/'timeline.sqlite3') as target:
                source.row_factory=sqlite3.Row
                cards=list(source.execute('SELECT * FROM memory_cards WHERE case_id IN ('+','.join('?' for _ in ids)+')',ids))
                for card in cards:
                    value=dict(card);value['user_id']=uid
                    metadata=json.loads(value['metadata']);metadata['diaryProvenance']='Imported prior demonstration memory, not new user confirmation';value['metadata']=json.dumps(metadata)
                    target.execute('INSERT INTO memory_cards ('+','.join(value)+') VALUES ('+','.join('?' for _ in value)+')',list(value.values()))
                report['importedMemoryCards']=len(cards)
            inventory=collect(runtime/'users'/uid,runtime/'timeline.sqlite3',uid,period_for('monthly','2026-09-16','Asia/Shanghai'))
            report['coverage']=inventory['coverage'];print(json.dumps({'coverage':report['coverage']},ensure_ascii=True),flush=True)
            save(out/'verification.json',report)
            for kind in ('daily','weekly','monthly'):
                response=await client.post('/api/diary',json={'kind':kind,'anchor':'2026-09-16','timezone':'Asia/Shanghai'})
                assert response.status==202,await response.text()
                row=await response.json();identifier=row['id'];last=None
                async with asyncio.timeout(1200):
                    while row['status'] in {'queued','running'}:
                        await asyncio.sleep(2)
                        row=await (await client.get('/api/diary/'+identifier)).json()
                        if row.get('phase')!=last:
                            last=row.get('phase');print(json.dumps({'kind':kind,'phase':last,'calls':len(row.get('calls',[]))},ensure_ascii=True),flush=True)
                save(out/(kind+'.json'),row);report['results'].append({'id':identifier,'kind':kind,'status':row['status'],'calls':len(row.get('calls',[])),'error':row.get('error')})
                save(out/'verification.json',report)
                assert row['status']=='completed',row.get('error')
                assert all(c['providerRequestId'] for c in row['calls'])
                assert row['coverage']['omittedRecords']==0
                assert (await client.get('/api/diary/'+identifier+'/export')).status==200
            await client.post('/api/sessions')
            listing=await (await client.get('/api/diary')).json()
            assert {r['id'] for r in report['results']}<={r['id'] for r in listing['entries']}
            report['crossSessionArchive']=True
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(channel='msedge',headless=True)
                context=await browser.new_context(viewport={'width':1440,'height':1000},record_video_dir=str(out/'recordings'))
                page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(str(client.make_url('/diary.html')))
                await page.locator('#login').wait_for(state='visible')
                await page.locator('[name=username]').fill(credentials['username']);await page.locator('[name=password]').fill(credentials['password'])
                await page.locator('#loginForm button').click()
                await page.wait_for_function('document.querySelectorAll(".claim").length>0')
                for item in report['results']:
                    await page.locator('[data-id="'+item['id']+'"]').click()
                    await page.wait_for_function('kind=>document.querySelector("#reading .eyebrow")?.textContent===kind',arg={'daily':'日记','weekly':'周回顾','monthly':'月回顾'}[item['kind']])
                    await page.screenshot(path=str(out/(item['kind']+'-desktop.png')),full_page=True)
                await page.locator('.claim details summary').first.click()
                await page.screenshot(path=str(out/'source-expanded.png'),full_page=True)
                feedback=page.locator('[data-feedback]').first
                if await feedback.count():
                    await feedback.click();await page.wait_for_function('document.querySelector("[data-feedback]")?.getAttribute("aria-pressed")==="true"')
                await page.reload();await page.wait_for_function('document.querySelectorAll(".claim").length>0')
                report['reloadRestoresArchive']=True
                await page.set_viewport_size({'width':390,'height':1000})
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert await page.locator('#reading').evaluate('(n)=>n.getBoundingClientRect().top')<await page.locator('aside').evaluate('(n)=>n.getBoundingClientRect().top')
                await page.screenshot(path=str(out/'monthly-mobile.png'),full_page=True)
                report['mobileReadingFirst']=True;report['pageErrors']=errors;assert not errors
                await context.close();await browser.close()
            report['passed']=True
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)[:800]
    save(out/'verification.json',report)
    print(json.dumps(report,ensure_ascii=True),flush=True)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args().output.resolve())) else 1)
