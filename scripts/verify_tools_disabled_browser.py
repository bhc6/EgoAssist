"""Real greetings in an active Skill with tool selection/execution disabled."""
import argparse
import asyncio
from contextlib import AsyncExitStack
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


async def verify(output,base_url=None):
    output.mkdir(parents=True,exist_ok=False)
    load_env(str(ROOT/'.env'))
    report={'passed':False,'realProviders':True,'pageErrors':[],'turns':[]}
    try:
        async with AsyncExitStack() as stack:
            if base_url:
                base=base_url.rstrip('/')
            else:
                server=await stack.enter_async_context(TestServer(create_app(str(BUILTIN_SKILLS_ROOT),None,
                    str(output/'runtime.json'),'qwen3.5-omni-flash-realtime','Ethan')))
                base=str(server.make_url('')).rstrip('/')
            pw=await stack.enter_async_context(async_playwright())
            browser=await pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=await browser.new_page()
                page.on('pageerror',lambda error:report['pageErrors'].append(str(error)))
                await page.goto(base+'/')
                await page.locator('#registerMode').click()
                await page.locator('#authUsername').fill('tools-off-'+secrets.token_hex(4))
                await page.locator('#authPassword').fill(secrets.token_urlsafe(24))
                await page.locator('#authSubmit').click()
                await page.wait_for_function('state?.sessionId')
                sid=report['sessionId']=await page.evaluate('selectedSessionId')
                response=await page.request.post(base+'/api/skills/activate',data={'sessionId':sid,'skill':'assembly-coach'})
                assert response.ok
                await page.reload()
                await page.wait_for_function('state?.sessionId')
                await page.evaluate('async()=>{await connectRealtime({media:false});}')
                await page.evaluate('''()=>{window.toolOffEvents=[];socket.addEventListener('message',m=>{
                    const e=JSON.parse(m.data);if(e.type==='state'||e.type==='memory_trace')return;
                    if(e.audio){e.audioBytes=e.audio.length;delete e.audio;}
                    toolOffEvents.push({...e,atMs:performance.now()});});}''')
                for question,expected in [('你好','你好'),('七加八是多少？只回答结果。','15')]:
                    await page.locator('#textTurnInput').fill(question)
                    started=await page.evaluate('performance.now()')
                    await page.locator('#textTurnSend').click()
                    tid=await page.evaluate('currentTurnId')
                    await page.wait_for_function("tid=>toolOffEvents.some(e=>e.type==='audio'&&e.origin?.turnId===tid)",arg=tid,timeout=45000)
                    await page.wait_for_function("tid=>toolOffEvents.some(e=>e.type==='assistant_transcript'&&e.done&&e.origin?.turnId===tid)",arg=tid,timeout=45000)
                    events=await page.evaluate('tid=>toolOffEvents.filter(e=>e.origin?.turnId===tid||e.turnId===tid)',tid)
                    answer=next(e['text'] for e in events if e['type']=='assistant_transcript' and e.get('done'))
                    assert expected in answer or (expected=='15' and '十五' in answer)
                    report['turns'].append({'question':question,'answer':answer,'firstAudioMs':next(e['atMs'] for e in events if e['type']=='audio')-started})
                    await page.wait_for_function('playbackSources.size===0',timeout=30000)
                audit=await (await page.request.get(base+'/api/prompts',params={'session':sid,'limit':100})).json()
                report['toolConfiguration']=audit['configuration']['tools']
                assert report['toolConfiguration']['enabled'] is False
                blocked=await page.request.post(base+'/api/tools/execute',data={'sessionId':sid,'tool':'show_reference','arguments':{}})
                assert blocked.status==400 and '工具调用已关闭' in (await blocked.json())['error']
                report['directExecutionBlocked']=True
                state=await (await page.request.get(base+'/api/state',params={'session':sid})).json()
                report['activeSkill']=state.get('activeSkill')
                assert report['activeSkill']['name']=='assembly-coach'
                # Read audit JSONL, without constructing a journal or replaying work.
                root=ROOT/'.egoassist' if base_url else output
                path=next(root.glob('users/*/sessions/'+sid+'.jsonl'))
                rows=[json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line.strip()]
                report['toolEvents']=[r['type'] for r in rows if r.get('type') in {'tool.request','tool.selection','tool_call','tool_execution'}]
                assert not report['toolEvents']
                report['routingModels']=[r['decision'].get('modelsUsed') for r in rows if r.get('type')=='control_decision']
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
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output,args.base_url)) else 1)
