"""Exercise durable conversation pagination and recovery in a real browser."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.product.conversation_history import conversation_page
from egoassist.realtime.work_ledger import CaseWorkLedger
from run_egoassist import create_app


async def verify(output, source):
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'pageErrors': [], 'checks': []}
    if source:
        # Parse a frozen copy; never alter the user's journal or recover their jobs.
        raw = source.read_bytes()
        frozen = output/'source-snapshot.jsonl'
        frozen.write_bytes(raw)
        journal = SessionJournal(frozen)
        full = conversation_page(journal, limit=200)
        report['realJournalAudit'] = {'source': str(source.resolve()), 'total': full['total'],
            'user': sum(r['role'] == 'user' for r in full['messages']),
            'assistant': sum(r['role'] == 'assistant' for r in full['messages']),
            'interrupted': sum(r['status'] == 'interrupted' for r in full['messages'])}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output/'runtime.json'), 'test', 'Ethan')
    async with TestServer(app) as server, async_playwright() as pw:
        browser = await pw.chromium.launch(channel='msedge', headless=True)
        try:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda exc: report['pageErrors'].append(str(exc)))
            await page.goto(str(server.make_url('/')))
            auth = await page.request.post(str(server.make_url('/api/auth/register')),
                data={'username': 'history-browser', 'password': 'history-browser-test-123'})
            uid = (await auth.json())['user']['id']
            response = await page.request.post(str(server.make_url('/api/sessions')), data={'label': '完整记录验证'})
            sid = (await response.json())['session']['id']
            journal = SessionJournal(output/'users'/uid/'sessions'/(sid+'.jsonl'), session_id=sid)
            ledger = CaseWorkLedger(journal)
            long_text = '这是一段保留换行的长回答。\n' * 180 + '<script>window.injected=true</script>'
            for i in range(125):
                turn = ledger.capture_turn(f'历史问题 {i:03}', source='input-audio-transcript', sequence=i, turn_id=f't{i}')
                ledger.set_turn_status(turn['turnId'], 'completed')
            journal.append('runtime_event', {'eventType': 'response.created', 'responseId': 'partial', 'payload': {'origin': {'turnId': 't124'}}})
            journal.append_message('assistant', '这句话说到一半被打断，仍应保留。', status='interrupted', source='provider-output', response_id='partial')
            journal.append_message('assistant', '已保存你的偏好。', source='server-memory-policy', response_id='receipt')
            journal.append('answer.job', {'turnId': 't124', 'status': 'completed', 'channel': 'background-text', 'text': long_text})
            await page.reload()
            await page.wait_for_function("document.querySelectorAll('#turnHistory .turn-history-row').length === 50")
            await page.wait_for_function("(() => {const p=document.querySelector('#turnHistory');return p.scrollHeight-p.scrollTop-p.clientHeight<2;})()")
            report['checks'].append('initial history opens at the latest message')
            await page.locator('#olderConversation').click()
            await page.wait_for_function("document.querySelectorAll('#turnHistory .turn-history-row').length === 100")
            await page.locator('#olderConversation').click()
            await page.wait_for_function("document.querySelectorAll('#turnHistory .turn-history-row').length === 128")
            assert await page.locator('#olderConversation').is_hidden()
            texts = await page.locator('#turnHistory').inner_text()
            assert '历史问题 000' in texts and '已中断' in texts and '已保存你的偏好' in texts
            assert await page.locator('#turnHistory [data-message-id="background:t124"] p').text_content() == long_text
            assert not await page.evaluate('Boolean(window.injected)')
            report['checks'].append('128 messages, interrupted speech, receipt, full long answer, safe HTML rendering')
            # A burst larger than one page must not leave a hidden gap.
            for i in range(55):
                journal.append_message('user', f'新增消息 {i}', source='fixture')
            await page.wait_for_function("document.querySelectorAll('#turnHistory .turn-history-row').length === 183")
            report['checks'].append('55 new messages without a pagination gap')
            await page.reload()
            await page.wait_for_function("document.querySelectorAll('#turnHistory .turn-history-row').length === 50")
            for expected in (100, 150, 183):
                await page.locator('#olderConversation').click()
                await page.wait_for_function('(n) => document.querySelectorAll("#turnHistory .turn-history-row").length === n', arg=expected)
            report['checks'].append('reload restores all persisted messages')
            await page.evaluate("""() => {
                conversationHistoryEvent({type:'assistant_transcript', responseId:'stream-test', purpose:'conversation', text:'保留', done:false});
                conversationHistoryEvent({type:'assistant_transcript', responseId:'stream-test', purpose:'conversation', text:'实时片段', done:false});
                conversationHistoryEvent({type:'response_status', responseId:'stream-test', status:'interrupted'});
            }""")
            live = page.locator('#turnHistory [data-message-id="assistant:stream-test"]')
            assert '保留实时片段' in await live.inner_text() and '已中断' in await live.inner_text()
            journal.append_message('assistant', '保留实时片段', status='interrupted', source='provider-output', response_id='stream-test')
            await page.wait_for_function("document.querySelector('#conversationCount').textContent.includes('184 / 184')")
            assert await live.count() == 1
            report['checks'].append('live deltas retained on interruption and deduplicated after persistence')
            response = await page.request.post(str(server.make_url('/api/sessions')), data={'label': '另一会话'})
            other_sid = (await response.json())['session']['id']
            other = SessionJournal(output/'users'/uid/'sessions'/(other_sid+'.jsonl'), session_id=other_sid)
            other.append_message('user', '只属于另一会话', source='fixture')
            await page.evaluate('(sid) => refreshSessionList(sid)', sid)
            await page.locator('#sessionSelect').select_option(other_sid)
            await page.wait_for_function("document.querySelector('#turnHistory').textContent.includes('只属于另一会话')")
            assert '历史问题' not in await page.locator('#turnHistory').inner_text()
            await page.locator('#sessionSelect').select_option(sid)
            await page.wait_for_function("document.querySelector('#conversationCount').textContent.includes('50 / 184')")
            assert '只属于另一会话' not in await page.locator('#turnHistory').inner_text()
            report['checks'].append('session switching clears old and live records')
            # Reading old messages must not hide new arrivals without an affordance.
            await page.locator('#turnHistory').evaluate('(p)=>{p.scrollTop=0;}')
            await page.wait_for_function("!document.querySelector('#latestConversation').hidden")
            before_scroll = await page.locator('#turnHistory').evaluate('(p)=>p.scrollTop')
            await page.evaluate("conversationHistoryEvent({type:'user_transcript', turnId:'live-voice', text:'你好，刚刚说过的话', channel:'input_audio_transcription', timestamp:'2026-09-25T15:23:49Z', persisted:true})")
            assert await page.locator('#turnHistory [data-message-id="user:live-voice"]').count() == 1
            assert await page.locator('#turnHistory').evaluate('(p)=>p.scrollTop') == before_scroll
            assert '1 条新消息' in await page.locator('#latestConversation').inner_text()
            await page.locator('#latestConversation').click()
            await page.wait_for_function("(() => {const p=document.querySelector('#turnHistory');return p.scrollHeight-p.scrollTop-p.clientHeight<2;})()")
            assert await page.locator('#latestConversation').is_hidden()
            ledger.capture_turn('你好，刚刚说过的话',source='input-audio-transcript',turn_id='live-voice')
            await page.wait_for_function("document.querySelector('#conversationCount').textContent.includes('/ 185')")
            assert await page.locator('#turnHistory [data-message-id="user:live-voice"]').count() == 1
            report['checks'].append('voice receipt appears immediately; reading position, new-message notice and durable deduplication work')
            await page.locator('#turnHistory').evaluate('(p)=>{p.scrollTop=0;}')
            await page.wait_for_function("!document.querySelector('#latestConversation').hidden")
            await page.evaluate("conversationHistoryEvent({type:'user_transcript',turnId:'typed-greeting',channel:'explicit_control',text:'你好'})")
            await page.wait_for_function("(() => {const p=document.querySelector('#turnHistory');return p.scrollHeight-p.scrollTop-p.clientHeight<2;})()")
            assert await page.locator('#latestConversation').is_hidden()
            report['checks'].append('sending text returns to the latest conversation even when reading old messages')
            # Polling while another workspace is open must keep follow-latest intent.
            await page.goto(str(server.make_url('/'))+'#/tasks')
            await page.reload()
            await page.wait_for_function("document.querySelectorAll('#turnHistory .turn-history-row').length === 50")
            journal.append_message('user','后台页面期间的新消息',source='fixture')
            await page.wait_for_function("document.querySelector('#turnHistory').textContent.includes('后台页面期间的新消息')")
            await page.locator('.workspace-nav a[href="#/conversation"]').click()
            await page.wait_for_function("(() => {const p=document.querySelector('#turnHistory');return p.clientHeight>0 && p.scrollHeight-p.scrollTop-p.clientHeight<2;})()")
            report['checks'].append('returning from a hidden workspace follows the newest message')
            for width in (1440, 390):
                await page.set_viewport_size({'width': width, 'height': 1000})
                await page.locator('#turnHistory').scroll_into_view_if_needed()
                assert not await page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                await page.screenshot(path=str(output/f'history-{width}.png'))
            report['checks'].append('desktop and mobile without page overflow')
            assert not report['pageErrors']
            report['passed'] = True
        finally:
            await browser.close()
            (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.output, args.source))
