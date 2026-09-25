"""Real diary generation and three-lane browser flows, in isolated account data.

Includes a separately labelled single HTTP failure injection for recovery only.
No model output, lane snapshot, or completed diary is mocked.
"""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
from contextlib import AsyncExitStack

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(output, base_url=None):
    output.mkdir(parents=True,exist_ok=False)
    load_env(str(ROOT/'.env'))
    report={'passed':False,'realProvider':True,'hardwareMeasured':False,'pageErrors':[]}
    credentials={'username':'lanes-diary-'+secrets.token_hex(3),'password':secrets.token_urlsafe(24)}
    try:
        async with AsyncExitStack() as stack:
            if base_url:
                base=base_url.rstrip('/')
            else:
                app=create_app(str(BUILTIN_SKILLS_ROOT),None,str(output/'runtime/runtime.json'),'qwen3.5-omni-flash-realtime','Ethan')
                server=await stack.enter_async_context(TestServer(app))
                base=str(server.make_url('')).rstrip('/')
            pw=await stack.enter_async_context(async_playwright())
            browser=await pw.chromium.launch(channel='msedge',headless=True)
            context=await browser.new_context(viewport={'width':1440,'height':1000},timezone_id='Asia/Shanghai')
            page=await context.new_page()
            page.on('pageerror',lambda error:report['pageErrors'].append(str(error)))
            await page.goto(base+'/')
            await page.locator('#registerMode').click()
            await page.locator('#authUsername').fill(credentials['username'])
            await page.locator('#authPassword').fill(credentials['password'])
            await page.locator('#authSubmit').click()
            await page.locator('#authGate').wait_for(state='hidden')
            await page.wait_for_function('Boolean(state?.runtimeLanes)')
            assert await page.locator('#executionLanes [data-lane]').count()==3
            await page.locator('.workspace-nav a[href="#/lanes"]').click()
            await page.locator('#laneDetails article').first.wait_for(state='visible')
            assert '未开启' in await page.locator('#lane-observation').text_content()
            async with page.expect_popup() as popup:
                await page.locator('.workspace-nav a[href="diary.html"]').click()
            diary=await popup.value
            diary.on('pageerror',lambda error:report['pageErrors'].append(str(error)))
            await diary.locator('#app').wait_for(state='visible')
            await diary.locator('#savedNotes h3').wait_for()
            # Save through the actual form, then reload: previously notes vanished from view.
            await diary.locator('#noteText').fill('今天上午完成桌面原型检查。下午计划整理参考资料，目前尚未完成。')
            await diary.locator('#noteForm button[type="submit"], #noteForm button:not([type])').click()
            await diary.wait_for_function('document.querySelector("#savedNotes").textContent.includes("桌面原型检查")')
            await diary.reload()
            await diary.wait_for_function('document.querySelector("#savedNotes").textContent.includes("桌面原型检查")')
            report['notesSurviveReload']=True
            async with diary.expect_response(lambda r:r.url==base+'/api/diary' and r.request.method=='POST') as accepted:
                await diary.locator('#generateButton').click()
            row=await (await accepted.value).json();identifier=row['id']
            # Wait for a real provider call to begin, then append while the snapshot runs.
            async with asyncio.timeout(30):
                while True:
                    result=await (await context.request.get(base+'/api/diary/'+identifier)).json()
                    if result.get('sourceManifest') is not None:raise AssertionError('Internal manifest leaked')
                    if result['status']=='running' and result.get('snapshotAt'):break
                    assert result['status'] in {'queued','running'},result.get('error')
                    await asyncio.sleep(.2)
            day=await diary.locator('#noteDay').input_value()
            added=await context.request.post(base+'/api/diary/notes',data={'day':day,'text':'下午新增：参考资料现在已整理完成。'})
            assert added.status==201
            await page.wait_for_function('state?.runtimeLanes?.memory?.accountDiary?.jobs?.running===1')
            assert '整理日记中' in await page.locator('#executionLanes [data-lane="memory"]').text_content()
            await page.screenshot(path=str(output/'lanes-running-1440.png'),full_page=True)
            # Check auto-poll recovery after one 503, explicitly separate from model evidence.
            injected=[]
            async def transient(route):
                if not injected:
                    injected.append(True)
                    await route.fulfill(status=503,content_type='application/json',body='{"error":"验收注入：一次临时连接失败"}')
                else:await route.continue_()
            await diary.route('**/api/diary/'+identifier,transient)
            await diary.wait_for_function('document.querySelector("#notice").classList.contains("error")',timeout=15000)
            assert await diary.locator('#retryLoad').is_visible()
            await diary.locator('#retryLoad').click()
            async with asyncio.timeout(240):
                while True:
                    result=await (await context.request.get(base+'/api/diary/'+identifier)).json()
                    if result['status'] not in {'queued','running'}:break
                    await asyncio.sleep(1)
            (output/'diary-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            assert result['status']=='completed',result.get('error')
            assert result['newRecords']>=1
            assert result['calls'] and all(call['providerRequestId'] for call in result['calls'])
            assert len(result['sources'])==1 and result['coverage']['records']==1
            await diary.wait_for_function('document.querySelectorAll("#reading .claim").length>0',timeout=20000)
            await diary.unroute('**/api/diary/'+identifier,transient)
            report['transientReadFailureRecovered']=bool(injected)
            report['snapshotCompletedDuringAppend']=True
            report['providerCalls']=[{'model':c['model'],'requestId':c['providerRequestId'],'latencyMs':c['latencyMs']} for c in result['calls']]
            await diary.reload()
            await diary.wait_for_function('document.querySelectorAll("#reading .claim").length>0')
            assert '快照后新增' in await diary.locator('#reading').text_content()
            await diary.locator('.claim details summary').first.click()
            assert '桌面原型检查' in await diary.locator('.claim .source').first.text_content()
            exported=await context.request.get(base+'/api/diary/'+identifier+'/export')
            assert exported.status==200
            report['reloadSourceAndExport']=True
            feedback=diary.locator('[data-feedback]').first
            if await feedback.count():
                await feedback.click()
                await diary.wait_for_function('document.querySelector("[data-feedback]").getAttribute("aria-pressed")==="true"')
                report['feedbackSaved']=True
            # Real interaction and memory states are rendered from the actual runtime.
            await page.locator('.workspace-nav a[href="#/conversation"]').click()
            await page.locator('#textTurnInput').fill('请记住：我的储物柜编号是482719。')
            await page.locator('#textTurnSend').click()
            await page.wait_for_function('state?.episodicMemory?.timelineViews?.participant?.recent.some(row=>row.text.includes("482719"))',timeout=45000)
            await page.wait_for_function('state?.workLedger?.turnInbox.some(row=>row.delivery?.status==="completed")',timeout=45000)
            await page.locator('.workspace-nav a[href="#/lanes"]').click()
            assert '482719' in await page.locator('#lane-interaction').text_content()
            assert '尚无已完成回答' not in await page.locator('#lane-interaction').text_content()
            assert '482719' in await page.locator('#lane-memory').text_content()
            assert '未开启' in await page.locator('#lane-observation').text_content()
            await page.locator('.workspace-nav a[href="#/conversation"]').click()
            await page.locator('#textTurnInput').fill('我的储物柜编号是什么？只回答数字。')
            await page.locator('#textTurnSend').click()
            await page.wait_for_function('state?.workLedger?.turnInbox.some(row=>row.text?.includes("只回答数字") && row.answer?.status==="completed" && row.answer.text.includes("482719"))',timeout=45000)
            await page.locator('.workspace-nav a[href="#/lanes"]').click()
            assert '482719' in await page.locator('#lane-interaction').text_content()
            report['realInteractionAndMemoryRendered']=True
            for width,height in ((1440,1000),(390,844)):
                await page.set_viewport_size({'width':width,'height':height})
                await page.locator('.workspace-nav a[href="#/conversation"]').click()
                await page.evaluate('scrollTo(0,0)')
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert await page.locator('#textTurnSend').evaluate('(n)=>n.getBoundingClientRect().bottom')<height
                await page.screenshot(path=str(output/f'conversation-{width}.png'),full_page=True)
                await page.locator('.workspace-nav a[href="#/lanes"]').click()
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await page.screenshot(path=str(output/f'lanes-{width}.png'),full_page=True)
                await diary.set_viewport_size({'width':width,'height':height})
                assert await diary.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await diary.screenshot(path=str(output/f'diary-{width}.png'),full_page=True)
            # Failed login must show outside the hidden app, not leave a silent screen.
            guest=await browser.new_context()
            login=await guest.new_page();await login.goto(base+'/diary.html')
            await login.locator('#login').wait_for(state='visible')
            await login.locator('[name=username]').fill(credentials['username'])
            await login.locator('[name=password]').fill('wrong-password')
            await login.locator('#loginForm button').click()
            await login.locator('#notice.error').wait_for(state='visible')
            assert await login.locator('#app').is_hidden()
            report['loginErrorVisible']=True
            await guest.close()
            assert not report['pageErrors'],report['pageErrors']
            report['passed']=True
            await context.close();await browser.close()
    except Exception as error:
        report['error']=type(error).__name__+': '+str(error)[:1600]
    (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--base-url')
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output.resolve(),args.base_url)) else 1)
