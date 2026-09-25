"""Real frame inference and reminder audio through the browser, without hardware claims."""
import argparse
import asyncio
import base64
from contextlib import AsyncExitStack
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from verify_shared_scene import decode


async def verify(output, base_url=None):
    load_env(str(ROOT/'.env'))
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'providerMock': False, 'hardwareMeasured': False, 'pageErrors': []}
    source = ROOT/'data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4'
    _, image = decode(source)
    report.update(source=str(source), sha256=hashlib.sha256(image).hexdigest())
    try:
        async with AsyncExitStack() as stack:
            if base_url:
                base = base_url.rstrip('/')
            else:
                app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output/'runtime.json'), 'qwen3.5-omni-flash-realtime', 'Ethan')
                server = await stack.enter_async_context(TestServer(app))
                base = str(server.make_url('')).rstrip('/')
            pw = await stack.enter_async_context(async_playwright())
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda e: report['pageErrors'].append(str(e)))
                await page.goto(base+'/')
                await page.locator('#registerMode').click()
                await page.locator('#authUsername').fill('reminder-'+secrets.token_hex(4))
                await page.locator('#authPassword').fill(secrets.token_urlsafe(24))
                await page.locator('#authSubmit').click()
                await page.wait_for_function('state?.sessionId')
                report['sessionId'] = await page.evaluate('selectedSessionId')
                await page.locator('#ambientGrant').click()
                await page.wait_for_function('ambientState.consented && !ambientPanel.busy')
                await page.locator('#ambientSettings > summary').click()
                assert await page.locator('#ambientReminders').is_checked()
                assert not await page.locator('#ambientQuestions').is_checked()
                async def settings(goal, enabled=True):
                    await page.locator('#ambientReminderGoal').fill(goal)
                    await page.locator('#ambientInterval').fill('2')
                    await page.locator('#ambientQuestionCooldown').fill('1')
                    await page.locator('#ambientReminders').set_checked(enabled)
                    await page.locator('#ambientSave').click()
                    await page.wait_for_function('!ambientPanel.busy && !ambientPanel.dirty')
                await settings('看到桌上的红白格桌布时提醒我检查笔记本。')
                await page.evaluate('async()=>{await connectRealtime({media:false});}')
                await page.evaluate("""() => {
                    window.reminderEvents = [];
                    socket.addEventListener('message', event => {
                        const m = JSON.parse(event.data);
                        if (m.origin?.observationReviewId && ['audio','assistant_transcript','response_status'].includes(m.type))
                            reminderEvents.push({type:m.type, responseId:m.responseId, origin:m.origin, text:m.text, done:m.done, at:Date.now()});
                    });
                }""")
                async def observe():
                    previous = await page.evaluate('state.runtimeLanes.observation.reviews.at(-1)?.candidateId || null')
                    started = time.perf_counter()
                    await page.evaluate("image=>socket.send(JSON.stringify({type:'video',image}))", base64.b64encode(image).decode())
                    await page.wait_for_function('id=>Boolean(state.runtimeLanes.observation.reviews.at(-1)?.candidateId) && state.runtimeLanes.observation.reviews.at(-1).candidateId !== id', arg=previous, timeout=45000)
                    row = await page.evaluate('state.runtimeLanes.observation.reviews.at(-1)')
                    return row, round((time.perf_counter()-started)*1000, 1)
                first, elapsed = await observe()
                report['firstReview'] = first
                report['reviewMs'] = elapsed
                assert first['notification'] and first['status'] in {'ready','audio-sent','played'}, first
                await page.wait_for_function("id=>state.runtimeLanes.observation.reviews.some(r=>r.candidateId===id && ['audio-sent','played'].includes(r.status))", arg=first['candidateId'], timeout=30000)
                await page.wait_for_function("reminderEvents.some(e=>e.type==='audio')", timeout=15000)
                await page.wait_for_function("reminderEvents.some(e=>e.type==='assistant_transcript' && e.text)", timeout=15000)
                report['delivery'] = await page.evaluate('id=>state.runtimeLanes.observation.reviews.find(r=>r.candidateId===id)', first['candidateId'])
                assert not report['delivery'].get('draftId')
                report['events'] = await page.evaluate('reminderEvents')
                transcripts = [e for e in report['events'] if e['type']=='assistant_transcript']
                report['spokenText'] = next((e['text'] for e in reversed(transcripts) if e.get('done')),
                    ''.join(e.get('text','') for e in transcripts))
                await page.wait_for_function('!state.runtimeLanes.interaction.scheduler.pending && !state.runtimeLanes.interaction.scheduler.activeResponseIds.length && playbackSources.size===0', timeout=30000)
                await asyncio.sleep(2.1)
                second, _ = await observe()
                report['repeatReview'] = second
                assert second['status'] not in {'ready','audio-sent','played'}, second
                assert len(await page.evaluate("[...new Set(reminderEvents.filter(e=>e.type==='audio').map(e=>e.responseId))]")) == 1
                # A condition absent from the scene must not trigger an unsolicited reminder.
                await settings('只有看到水壶里的水明显沸腾时提醒我关火；没有水壶就不要提醒。')
                await asyncio.sleep(2.1)
                negative, _ = await observe()
                report['irrelevantGoal'] = negative
                assert not negative['notification'] and negative['status'] != 'ready', negative
                await settings('看到桌上的红白格桌布时提醒我收好笔记本。', False)
                await asyncio.sleep(2.1)
                disabled, _ = await observe()
                report['disabledReview'] = disabled
                assert disabled['status'] not in {'ready','audio-sent','played'}, disabled
                await page.locator('#ambientRevoke').click()
                await page.wait_for_function('!ambientState.consented && !ambientPanel.busy')
                await page.locator('#textTurnInput').fill('九乘九是多少？只回答数字。')
                await page.locator('#textTurnSend').click()
                await page.wait_for_function('state.workLedger.turnInbox.some(t=>t.text.includes("九乘九") && t.answer?.status==="completed" && t.answer.text.includes("81"))', timeout=60000)
                report['dialogueAfterStop'] = True
                await page.locator('#ambientGrant').click()
                await page.wait_for_function('ambientState.consented && !ambientPanel.busy')
                await page.locator('#ambientResults').evaluate('e=>e.open=true')
                await page.locator('[data-disclosure="observation-reviews"]').evaluate('e=>e.open=true')
                for width in (1440, 390):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await page.locator('#ambientBanner').scroll_into_view_if_needed()
                    await page.screenshot(path=str(output/f'reminders-{width}.png'), full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                await page.locator('#ambientRevoke').click()
                await page.wait_for_function('!ambientState.consented && !ambientPanel.busy')
                assert not report['pageErrors']
                report['passed'] = True
            finally:
                await browser.close()
    except Exception as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
        report['traceback'] = traceback.format_exc()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:report.get(k) for k in ('passed','error','reviewMs','spokenText')},ensure_ascii=True),flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--base-url')
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output,args.base_url)) else 1)
