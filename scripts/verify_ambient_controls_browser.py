"""Ambient controls through the actual browser, with optional real frame inference.

The offline test injects stale UI snapshots and one failed HTTP response only to
verify control robustness. --real uses recorded EgoLife frames, not a camera.
"""
import argparse
import asyncio
import base64
from contextlib import AsyncExitStack
import io
import json
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(output, real=False, base_url=None):
    output.mkdir(parents=True,exist_ok=False)
    if real:load_env(str(ROOT/'.env'))
    report={'passed':False,'realProvider':real,'hardwareMeasured':False,'pageErrors':[],
            'faultInjection':['stale UI snapshot','one settings HTTP 503']}
    try:
        async with AsyncExitStack() as stack:
            if base_url:
                base=base_url.rstrip('/')
            else:
                app=create_app(str(BUILTIN_SKILLS_ROOT),None,str(output/'runtime.json'),'qwen3.5-omni-flash-realtime','Ethan')
                server=await stack.enter_async_context(TestServer(app));base=str(server.make_url('')).rstrip('/')
            pw=await stack.enter_async_context(async_playwright())
            browser=await pw.chromium.launch(channel='msedge',headless=True)
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            page=await context.new_page();page.on('pageerror',lambda e:report['pageErrors'].append(str(e)))
            await page.goto(base+'/')
            await page.locator('#registerMode').click()
            await page.locator('#authUsername').fill('ambient-ui-'+secrets.token_hex(3))
            await page.locator('#authPassword').fill(secrets.token_urlsafe(24))
            await page.locator('#authSubmit').click();await page.wait_for_function('state?.sessionId')
            sid=await page.evaluate('selectedSessionId')
            async def snapshot():return await (await context.request.get(base+'/api/state',params={'session':sid})).json()
            await page.locator('#ambientGrant').click()
            await page.wait_for_function('ambientState?.consented && !ambientPanel.busy')
            assert not await page.locator('#ambientQuestions').is_checked()
            await page.locator('#ambientSettings > summary').click()
            await page.locator('#ambientInterval').fill('7')
            await page.locator('#ambientQuestions').check()
            await page.locator('#ambientQuestionCooldown').fill('120')
            await page.locator('#textTurnInput').focus()
            await page.evaluate('render({...state,ambient:{...state.ambient,processing:false}})')
            assert await page.locator('#ambientInterval').input_value()=='7'
            assert await page.locator('#ambientQuestions').is_checked()
            assert await page.locator('#ambientQuestionCooldown').input_value()=='120'
            await page.locator('#ambientSave').click()
            await page.wait_for_function('!ambientPanel.busy && document.querySelector("#ambientControlStatus").textContent.includes("设置已保存")')
            state=await snapshot();assert state['ambient']['intervalSeconds']==7 and state['ambient']['questionsEnabled']
            report['draftSurvivesRefreshAndApplies']=True
            report['independentPlaybackControls'] = await page.evaluate("""() => {
                const original = ambientState, stopped = [];
                const fake = id => ({egoassistResponseId:id, stop:()=>stopped.push(id)});
                const r=fake('test-reminder'), q=fake('test-question'), u=fake('test-user');
                observationResponseKinds.set('test-reminder','reminder');
                observationResponseKinds.set('test-question','clarification');
                playbackSources.add(r);playbackSources.add(q);playbackSources.add(u);
                acceptAmbient({...ambientState,questionsEnabled:false,remindersEnabled:true,controlRevision:ambientState.controlRevision+1});
                if (JSON.stringify(stopped)!==JSON.stringify(['test-question'])) throw Error('question switch stopped the wrong output');
                acceptAmbient({...ambientState,reminderGoal:'changed goal',controlRevision:ambientState.controlRevision+1});
                if (JSON.stringify(stopped)!==JSON.stringify(['test-question','test-reminder'])) throw Error('goal edit did not cancel old reminder');
                if (!playbackSources.has(u)) throw Error('user answer was stopped');
                playbackSources.delete(u);observationResponseKinds.clear();stoppedSpeechResponses.clear();
                ambientState=original;
                return true;
            }""")
            await page.reload();await page.wait_for_function('ambientState?.intervalSeconds===7')
            await page.locator('#ambientSettings > summary').click()
            assert await page.locator('#ambientQuestions').is_checked()
            await page.locator('#ambientQuestions').uncheck()
            await page.locator('#ambientReminders').uncheck()
            await page.locator('#ambientSave').click()
            await page.wait_for_function('!ambientPanel.busy && !ambientState.questionsEnabled')
            # One failed save must retain the draft and release all controls.
            async def fail(route):await route.fulfill(status=503,content_type='application/json',body='{"error":"验收注入：临时连接失败"}')
            await page.route('**/api/ambient/settings',fail,times=1)
            await page.locator('#ambientInterval').fill('9');await page.locator('#ambientSave').click()
            await page.wait_for_function('!ambientPanel.busy && document.querySelector("#ambientControlStatus").classList.contains("error")')
            assert await page.locator('#ambientInterval').input_value()=='9'
            assert await page.locator('#ambientSave').is_enabled()
            await page.locator('#ambientSave').click()
            await page.wait_for_function('ambientState.intervalSeconds===9 && !ambientPanel.busy')
            report['failedSaveRetainsDraftAndCanRetry']=True
            report['faultInjection'].extend(['state fetch aborted', 'settings fetch aborted before dispatch',
                                             'settings response lost after commit', 'consent response lost after commit'])
            # Network failures can occur before dispatch or after a committed write.
            # Poll recovery must reconcile actual state, never replay the mutation.
            async def abort_read(route): await route.abort('failed')
            await page.route('**/api/state?*',abort_read)
            await page.evaluate("document.querySelector('#textTurnError').textContent='保留对话错误'")
            await page.wait_for_function("document.querySelector('#stateConnectionStatus').textContent.includes('自动重连')",timeout=15000)
            assert await page.locator('#textTurnError').inner_text()=='保留对话错误'
            await page.unroute('**/api/state?*',abort_read)
            await page.wait_for_function("!document.querySelector('#stateConnectionStatus').textContent",timeout=15000)
            assert await page.locator('#textTurnError').inner_text()=='保留对话错误'
            report['stateNetworkFailureRecoversWithoutOverwritingDialogue']=True
            await page.route('**/api/ambient/settings',abort_read,times=1)
            await page.locator('#ambientInterval').fill('11');await page.locator('#ambientSave').click()
            await page.wait_for_function("document.querySelector('#ambientControlStatus').textContent.includes('尚未确认')",timeout=15000)
            assert await page.locator('#ambientInterval').input_value()=='11'
            assert (await snapshot())['ambient']['intervalSeconds']==9
            report['failedDispatchPreservesDraftAndDoesNotAutoRetry']=True
            async def lose_response(route):
                response=await route.fetch()
                assert response.ok
                await route.abort('failed')
            await page.route('**/api/ambient/settings',lose_response,times=1)
            await page.locator('#ambientSave').click()
            await page.wait_for_function("document.querySelector('#ambientControlStatus').textContent.includes('已确认设置生效')",timeout=15000)
            assert not await page.evaluate('ambientPanel.dirty')
            assert (await snapshot())['ambient']['intervalSeconds']==11
            report['lostSaveResponseReconcilesCommittedSettings']=True
            await page.route('**/api/ambient/consent',lose_response,times=1)
            await page.locator('#ambientRevoke').click()
            await page.wait_for_function("document.querySelector('#ambientControlStatus').textContent.includes('已确认后台观察停止')",timeout=15000)
            assert await page.locator('#ambientGrant').is_visible()
            report['lostStopResponseReconcilesConsent']=True
            await page.locator('#ambientGrant').click()
            await page.wait_for_function('ambientState.consented && !ambientPanel.busy')
            for _ in range(4):
                stale=await snapshot()
                await page.locator('#ambientRevoke').click()
                await page.wait_for_function('!ambientState.consented && !ambientPanel.busy')
                await page.evaluate('old=>render(old)',stale)
                assert await page.locator('#ambientGrant').is_visible()
                assert not await page.evaluate('ambientState.consented')
                await page.locator('#ambientGrant').click()
                await page.wait_for_function('ambientState.consented && !ambientPanel.busy')
                assert await page.locator('#ambientRevoke').is_visible()
                assert await page.locator('#ambientSave').is_enabled()
            report['repeatedStopStartAndStaleSnapshot']=True
            await page.locator('#ambientInterval').fill('2');await page.locator('#ambientSave').click()
            await page.wait_for_function('ambientState.intervalSeconds===2 && !ambientPanel.busy')
            if real:
                import av
                from egoassist.core.journal import SessionJournal
                media=ROOT/'data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4'
                frames=[]
                with av.open(str(media)) as recording:
                    for frame in recording.decode(video=0):
                        if frame.time is not None and frame.time>=(3,10,20)[len(frames)]:
                            buffer=io.BytesIO();frame.to_image().resize((768,432)).save(buffer,format='JPEG')
                            frames.append(base64.b64encode(buffer.getvalue()).decode())
                            if len(frames)==3:break
                await page.evaluate('async()=>{await connectRealtime({media:false});}')
                await page.evaluate('image=>socket.send(JSON.stringify({type:"video",image}))',frames[0])
                await page.wait_for_function('state?.ambient?.observation?.summary',timeout=45000)
                observed=await page.evaluate('state.ambient.observation')
                report['firstObservation']={'summary':observed['summary'],'frameId':observed['frameId']}
                await page.locator('#ambientRevoke').click()
                await page.wait_for_function('!ambientState.consented && !ambientPanel.busy')
                state=await snapshot()
                assert not state['ambient']['processing'] and not state['ambient']['lane']['pendingFrame']
                # Stream recorded frames for more than both old cadence windows.
                data_root=ROOT/'.egoassist' if base_url else output
                path=next((data_root/'users').glob('*/sessions/'+sid+'.jsonl'))
                def inference_requests():
                    return [r['id'] for r in SessionJournal(path).entries() if r.get('eventType')=='perception.response.requested' or r.get('type')=='scene.request']
                before=inference_requests()
                for index in range(7):
                    await page.evaluate('image=>socket.send(JSON.stringify({type:"video",image}))',frames[1+index%2])
                    await asyncio.sleep(1)
                assert inference_requests()==before, 'Stopping silently dispatched another vision request'
                assert not (await snapshot())['ambient']['processing']
                report['noBackgroundInferenceAfterStop']=True
                # Stop must not close dialogue or disable user-initiated image queries.
                await page.locator('#textTurnInput').fill('九乘九是多少？只回答数字。')
                await page.locator('#textTurnSend').click()
                await page.wait_for_function('state.workLedger.turnInbox.some(t=>t.text.includes("九乘九") && t.answer?.status==="completed" && t.answer.text.includes("81"))',timeout=45000)
                report['dialogueWorksAfterStop']=True
                await page.locator('#textTurnInput').fill('请描述我当前摄像头画面中桌布的颜色和图案，只依据画面回答。')
                await page.locator('#textTurnSend').click()
                await page.wait_for_function('state.workLedger.turnInbox.some(t=>t.text.includes("当前摄像头画面中桌布") && t.answer?.status==="completed" && t.answer.text.includes("红") && t.answer.text.includes("格"))',timeout=45000)
                assert not (await snapshot())['ambient']['consented']
                report['explicitVisualQuestionWorksAfterStop']=True
                await page.locator('#ambientGrant').click()
                await page.wait_for_function('ambientState.consented && !ambientPanel.busy')
                await page.evaluate('image=>socket.send(JSON.stringify({type:"video",image}))',frames[2])
                await page.wait_for_function('old=>state?.ambient?.observation?.frameId && state.ambient.observation.frameId!==old',arg=observed['frameId'],timeout=45000)
                report['realObservationRestarts']=True
                await page.locator('#ambientResults > summary').click()
            for width,height in ((1440,1000),(390,844)):
                await page.set_viewport_size({'width':width,'height':height})
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await page.screenshot(path=str(output/f'ambient-{width}.png'),full_page=True)
            await page.locator('#ambientRevoke').click();await page.wait_for_function('!ambientState.consented && !ambientPanel.busy')
            await page.reload();await page.wait_for_function('state?.ambient && !state.ambient.consented')
            assert await page.locator('#ambientGrant').is_visible()
            report['stopPersistsReload']=True
            assert not report['pageErrors'],report['pageErrors']
            report['passed']=True
            await context.close();await browser.close()
    except Exception as error:
        report['error']=type(error).__name__+': '+str(error)[:1800]
    (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--real',action='store_true');parser.add_argument('--base-url')
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output.resolve(),args.real,args.base_url)) else 1)
