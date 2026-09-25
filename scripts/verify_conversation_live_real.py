"""Real ASR receipts must reach history without waiting for history polling.

Uses synthetic speech through the actual provider; no physical microphone claim.
"""
import argparse
import asyncio
import base64
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from verify_real_voice_stop import synthesize


async def verify(output, base_url=None):
    output.mkdir(parents=True, exist_ok=False)
    load_env(str(ROOT/'.env'))
    report = {'passed':False, 'realProvider':True, 'hardwareMeasured':False, 'pageErrors':[]}
    try:
        pcm, report['fixture'] = await synthesize('你好，九乘九是多少？请简短回答。', output, 0)
        async with AsyncExitStack() as stack:
            if base_url:
                base = base_url.rstrip('/')
            else:
                app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output/'runtime.json'),
                                 'qwen3.5-omni-flash-realtime', 'Ethan')
                server = await stack.enter_async_context(TestServer(app))
                base = str(server.make_url('')).rstrip('/')
            pw = await stack.enter_async_context(async_playwright())
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
                await page.goto(base+'/')
                await page.locator('#registerMode').click()
                await page.locator('#authUsername').fill('live-history-'+secrets.token_hex(3))
                await page.locator('#authPassword').fill(secrets.token_urlsafe(24))
                await page.locator('#authSubmit').click()
                await page.wait_for_function('state?.sessionId')
                sid = await page.evaluate('selectedSessionId')
                report['sessionId'] = sid
                await page.evaluate('async()=>{await connectRealtime({media:false});}')
                await page.evaluate("""()=>{
                    window.historyReceipts=[];
                    socket.addEventListener('message',event=>{
                        const message=JSON.parse(event.data);
                        if(message.type==='user_transcript')historyReceipts.push(message);
                    });
                }""")
                # Deliberately return old HTTP history while real socket events arrive.
                async def stale_history(route):
                    await route.fulfill(status=200,content_type='application/json',
                        body=json.dumps({'messages':[],'total':0,'nextBefore':None}))
                await page.route('**/api/conversation?*',stale_history)
                silence_ms = max(2000,int(os.getenv('EGOASSIST_SPEECH_END_SILENCE_MS','1200'))+1000)
                data = pcm + bytes(silence_ms*32)
                for offset in range(0,len(data),3200):
                    await page.evaluate("audio=>socket.send(JSON.stringify({type:'audio',audio}))",
                                        base64.b64encode(data[offset:offset+3200]).decode())
                    await asyncio.sleep(.1)
                await page.wait_for_function('historyReceipts.some(e=>e.turnId && e.persisted)',timeout=45000)
                receipt = await page.evaluate('historyReceipts.find(e=>e.turnId && e.persisted)')
                report['voiceReceipt'] = receipt
                row = page.locator('#turnHistory [data-message-id="user:'+receipt['turnId']+'"]')
                assert await row.count()==1 and receipt['text'] in await row.inner_text()
                report['voiceVisibleWithoutHistoryPolling']=True
                await page.unroute('**/api/conversation?*',stale_history)
                await page.wait_for_function("document.querySelector('#conversationCount').textContent.includes('已保存消息') && !document.querySelector('#conversationCount').textContent.startsWith('已加载 0')")
                assert await row.count()==1
                report['voiceDeduplicatedAfterPolling']=True
                await page.wait_for_function("[...document.querySelectorAll('#turnHistory .completed-answer')].some(p=>/81|八十一/.test(p.textContent))",timeout=45000)
                report['voiceAnswerVisible']=True
                # Real acoustic pipeline; explicitly label only this isolated synthetic sample.
                await page.wait_for_function('state.sharedScene.speakers.length>0',timeout=45000)
                speech = await page.evaluate('id=>state.sharedScene.utterances.find(r=>r.turnId===id)?.speaker',receipt['turnId'])
                report['speakerAnalysis'] = speech
                assert speech and speech.get('speakerId'), speech
                await page.locator('#setupMyVoice').click()
                await page.locator('#speakerIdentitySelect').select_option(speech['speakerId'])
                await page.locator('#speakerIdentityName').fill('验收合成声音')
                await page.locator('#speakerIsWearer').check()
                await page.locator('#speakerIdentityForm button').click()
                await page.wait_for_function('id=>document.querySelector(`#turnHistory [data-message-id="user:${id}"]`)?.textContent.includes("本人 · 声音匹配")',arg=receipt['turnId'])
                report['confirmedSyntheticVoiceVisible'] = True
                report['speakerIdentityGroundTruth'] = False
                await page.locator('#textTurnInput').fill('你好')
                await page.locator('#textTurnSend').click()
                await page.wait_for_function("historyReceipts.some(e=>e.channel==='explicit_control' && e.text==='你好')",timeout=30000)
                typed = await page.evaluate("historyReceipts.find(e=>e.channel==='explicit_control' && e.text==='你好')")
                typed_row = page.locator('#turnHistory [data-message-id="user:'+typed['turnId']+'"]')
                assert await typed_row.count()==1
                report['typedGreetingVisible']=True
                await page.wait_for_function("state.workLedger.turnInbox.some(t=>t.text==='你好' && t.answer?.status==='completed')",timeout=45000)
                saved = await (await page.request.get(base+'/api/conversation',params={'session':sid})).json()
                assert len([r for r in saved['messages'] if r.get('turnId')==receipt['turnId'] and r['role']=='user'])==1
                assert len([r for r in saved['messages'] if r.get('turnId')==typed['turnId'] and r['role']=='user'])==1
                report['savedMessages']=saved['total']
                await page.reload()
                await page.wait_for_function("document.querySelector('#turnHistory').textContent.includes('你好')")
                assert await page.locator('#turnHistory [data-message-id="user:'+receipt['turnId']+'"]').count()==1
                assert '本人 · 声音匹配' in await page.locator('#turnHistory [data-message-id="user:'+receipt['turnId']+'"]').inner_text()
                assert '你的文字' in await page.locator('#turnHistory [data-message-id="user:'+typed['turnId']+'"]').inner_text()
                report['reloadPreservesBothInputs']=True
                for width in (1440,390):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await page.locator('#turnHistory').scroll_into_view_if_needed()
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await page.screenshot(path=str(output/f'history-{width}.png'))
                assert not report['pageErrors']
                report['passed']=True
            finally:
                await browser.close()
    except Exception as error:
        report['error']=type(error).__name__+': '+str(error)[:1800]
    (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--base-url')
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output,args.base_url)) else 1)
