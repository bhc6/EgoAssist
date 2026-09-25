"""Exercise the production UI in installed Chrome/Edge with isolated data.

No mocked endpoints or media. This verifies UI/account/session flows, not hardware.
Requires playwright; does not download a browser.
"""
import argparse
import asyncio
import json
import re
from datetime import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(output, real_turn=False):
    if real_turn:
        load_env(str(ROOT / '.env'))
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'pageErrors': [], 'failedRequests': [], 'viewports': [],
              'mock': False, 'hardwareMeasured': False}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / 'runtime.json'),
                     'qwen3.5-omni-flash-realtime', 'Ethan')
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
                page.on('requestfailed', lambda req: report['failedRequests'].append(req.url))
                await page.goto(str(server.make_url('/')))
                await page.locator('#registerMode').click()
                await page.locator('#authUsername').fill('browser-acceptance')
                await page.locator('#authPassword').fill('isolated-browser-account-123')
                await page.locator('#authSubmit').click()
                await page.locator('#authGate').wait_for(state='hidden')
                await page.locator('#textTurnInput').wait_for(state='visible')
                page.once('dialog', lambda dialog: dialog.accept('browser-real-session'))
                await page.locator('#newSession').click()
                await page.wait_for_function("document.querySelector('#sessionSelect').selectedOptions[0]?.textContent.includes('browser-real-session')")
                report['sessionCreated'] = True
                assert await page.locator('.workspace-nav a').nth(1).get_attribute('href') == 'diary.html'
                assert await page.locator('#insightKind').input_value() == 'suggestions'
                async with page.expect_popup() as diary_popup:
                    await page.locator('.workspace-nav a[href="diary.html"]').click()
                diary = await diary_popup.value
                diary.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
                await diary.locator('#app').wait_for(state='visible')
                assert await diary.locator('#login').is_hidden()
                assert await diary.locator('#kind').input_value() == 'daily'
                report['diarySharesLogin'] = True
                for width in (1440, 390):
                    await diary.set_viewport_size({'width': width, 'height': 1000})
                    assert await diary.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await diary.screenshot(path=str(output / f'diary-{width}.png'), full_page=True)
                await diary.close()
                report['recommendedNavigationDefaults'] = True
                await page.locator('a[href="#/tasks"]').click()
                await page.locator('#skillSelect').select_option('packing-coach')
                await page.wait_for_function("document.querySelector('#skillName').textContent.includes('packing-coach')")
                await page.locator('#taskSelect').select_option('commute-check')
                await page.wait_for_function("document.querySelector('#taskTitle').textContent.includes('通勤出门核对')")
                report['proceduralSkillSelected'] = True
                await page.locator('#skillSelect').select_option('field-notes')
                await page.wait_for_function("document.querySelector('#skillName').textContent.includes('field-notes')")
                assert await page.locator('#taskSelect').is_disabled()
                assert await page.locator('#newTask').is_disabled()
                assert '对话工作流' in await page.locator('#skillName').text_content()
                assert 'static-skill-policy-injection' in await page.locator('#knowledge').text_content()
                report['conversationalSkillReady'] = True
                await page.locator('a[href="#/conversation"]').click()
                assert await page.locator('[data-performance="routerMs"]').text_content() == '-'
                if real_turn:
                    async def wait_for_report(accepted):
                        job = accepted['job']
                        await page.wait_for_function("id => { const row = document.querySelector('[data-insight-job=\"' + id + '\"]'); return row && /已完成|无记录|失败/.test(row.textContent); }", arg=job['jobId'], timeout=60000)
                        payload = await (await page.request.get(str(server.make_url('/api/insights')) + '?session=' + await page.evaluate('selectedSessionId'))).json()
                        final_job = next(item for item in payload['jobs'] if item['jobId'] == job['jobId'])
                        assert final_job['status'] == 'completed', final_job
                        result = await (await page.request.get(str(server.make_url('/api/insights')) + '?session=' + payload['sessionId'] + '&result=' + final_job['resultEventId'])).json()
                        return {**result['results'][0], 'acceptedMs': accepted['acceptedMs'], 'job': final_job}

                    await page.locator('#textTurnInput').fill('请整理现场记录：今天张岚在会议室确认样机已经收到，验收尚未完成。')
                    await page.locator('#textTurnSend').click()
                    await page.wait_for_function("document.querySelector('[data-performance=\"responseFirstAudioMs\"]').textContent.includes('ms')", timeout=45000)
                    await page.wait_for_function("document.querySelector('#performanceUsage').textContent.includes('response.done')", timeout=45000)
                    report['assistantCaption'] = await page.locator('#assistantCaption').text_content()
                    assert await page.locator('#assistantCaption p').text_content()
                    report['performance'] = await page.evaluate('state.performance')
                    durable_cards = await page.evaluate('state.episodicMemory.timelineViews.participant.recent')
                    assert not any('张岚' in item.get('text', '') for item in durable_cards), 'Draft request became participant memory'
                    assert report['performance']['metrics']['routerMs']['value'] > 0
                    assert report['performance']['metrics']['responseFirstAudioMs']['responseId']
                    report['realTextTurn'] = True
                    report['insights'] = []
                    for kind in ('daily', 'weekly', 'monthly'):
                        await page.locator('a[href="#/reports"]').click()
                        await page.locator('#insightKind').select_option(kind)
                        async with page.expect_response(lambda response: response.url.endswith('/api/insights') and response.request.method == 'POST', timeout=60000) as pending:
                            await page.locator('#insightGenerate').click()
                        response = await pending.value
                        assert response.status == 202
                        accepted = await response.json()
                        if kind == 'daily':
                            active = await (await page.request.get(str(server.make_url('/api/insights')) + '?session=' + await page.evaluate('selectedSessionId'))).json()
                            report['concurrentReportStatus'] = next(j['status'] for j in active['jobs'] if j['jobId'] == accepted['job']['jobId'])
                            assert report['concurrentReportStatus'] in {'queued', 'running'}
                            previous = await page.evaluate('state.performance.latestUsage.responseId')
                            await page.locator('a[href="#/conversation"]').click()
                            await page.locator('#textTurnInput').fill('刚才的现场记录里，验收完成了吗？')
                            await page.locator('#textTurnSend').click()
                            await page.wait_for_function('previous => state.performance.latestUsage?.responseId !== previous', arg=previous, timeout=45000)
                            report['concurrentDialoguePerformance'] = await page.evaluate('state.performance')
                            report['concurrentDialogueAnswer'] = await page.locator('#assistantCaption p').text_content()
                            assert '未' in report['concurrentDialogueAnswer']
                        result = await wait_for_report(accepted)
                        report['insights'].append(result)
                        assert result['status'] == 'completed', result
                        assert result['coverage']['complete']
                        assert '张岚' in ''.join(item['text'] for item in result['summary']), result
                        assert '验收' in ''.join(item['text'] for item in result['summary']), result
                        expected_dates = {datetime.fromisoformat(source['recordedLocalAt']).date().isoformat()
                                          for source in result['sources']}
                        summary = ''.join(item['text'] for item in result['summary'])
                        # This fixture states "today" only, so explicit output dates
                        # must use the source's local day rather than its UTC day.
                        for year, month, day in re.findall(r'(\d{4})[年-](\d{1,2})[月-](\d{1,2})日?', summary):
                            assert f'{int(year):04d}-{int(month):02d}-{int(day):02d}' in expected_dates, result
                    await page.locator('a[href="#/reports"]').click()
                    async with page.expect_response(lambda response: response.url.endswith('/api/insights') and response.request.method == 'POST', timeout=60000) as pending:
                        await page.locator('#proactiveSuggestions').check()
                    suggestions = await wait_for_report(await (await pending.value).json())
                    report['automaticSuggestions'] = suggestions
                    assert suggestions['status'] == 'completed', suggestions
                    assert suggestions['suggestions'], suggestions
                    await page.locator('#proactiveSuggestions').uncheck()
                    await page.locator('a[href="#/audit"]').click()
                    await page.locator('#promptAuditDetails').evaluate('(node) => { node.open = true; }')
                    await page.wait_for_function("document.querySelector('#actualPromptRequests').textContent.includes('insight.request')")
                    report['realPromptAuditVisible'] = True
                    original_session = await page.evaluate('selectedSessionId')
                    await page.evaluate("socket.close(4001, 'acceptance-disconnect')")
                    await page.wait_for_function('providerReady && socket?.readyState === WebSocket.OPEN', timeout=30000)
                    assert await page.evaluate('selectedSessionId') == original_session
                    await page.locator('a[href="#/conversation"]').click()
                    await page.locator('#textTurnInput').fill('刚才的现场记录里，验收完成了吗？')
                    previous_response = await page.evaluate('state.performance.latestUsage.responseId')
                    await page.locator('#textTurnSend').click()
                    await page.wait_for_function('previous => state.performance.latestUsage?.responseId !== previous', arg=previous_response, timeout=45000)
                    assert '未' in await page.locator('#assistantCaption p').text_content(), await page.locator('#assistantCaption').text_content()
                    report['reconnectedWithoutLosingSession'] = True
                    # Finish an interrupted real answer under its original question,
                    # including when the browser reloads during generation.
                    await page.locator('#textTurnInput').fill('请用阿拉伯数字计算七乘八，之后详细解释乘法原理，至少讲十句话。')
                    await page.locator('#textTurnSend').click()
                    await page.wait_for_function('!document.querySelector("#textTurnSend").disabled && !document.querySelector("#textTurnInput").value')
                    old_turn = await page.evaluate('currentTurnId')
                    await page.wait_for_function('id => currentTurnId === id && captionResponseId && state.performance.metrics.responseFirstAudioMs?.responseId === captionResponseId', arg=old_turn, timeout=45000)
                    await page.locator('#textTurnInput').fill('另外一个独立问题：九乘九是多少？')
                    await page.locator('#textTurnSend').click()
                    await page.wait_for_function('id => state.workLedger.turnInbox.some(t => t.turnId === id && ["queued", "running", "completed"].includes(t.answer?.status) && t.answer?.channel !== "speech")', arg=old_turn, timeout=15000)
                    await page.reload()
                    await page.locator('#authGate').wait_for(state='hidden')
                    await page.wait_for_function('id => state?.workLedger?.turnInbox.some(t => t.turnId === id && t.answer?.status === "completed")', arg=old_turn, timeout=45000)
                    answer_row = page.locator('#turnWorkHistory [data-turn-id="' + old_turn + '"]')
                    assert '56' in await answer_row.locator('.completed-answer').text_content()
                    report['automaticAnswerAfterReload'] = await page.evaluate('id => state.workLedger.turnInbox.find(t => t.turnId === id).answer', old_turn)
                    assert report['automaticAnswerAfterReload']['providerRequestId']
                    await page.locator('#textTurnInput').fill('请详细解释乘法和除法的关系，至少讲二十句话。')
                    await page.locator('#textTurnSend').click()
                    await page.wait_for_function('!document.querySelector("#textTurnSend").disabled && !document.querySelector("#textTurnInput").value')
                    cancelled_turn = await page.evaluate('currentTurnId')
                    await page.wait_for_function('id => currentTurnId === id && captionResponseId && state.performance.metrics.responseFirstAudioMs?.responseId === captionResponseId', arg=cancelled_turn, timeout=45000)
                    await page.locator('#textTurnInput').fill('另外一个独立问题：三乘三是多少？')
                    await page.locator('#textTurnSend').click()
                    await page.locator('#turnWorkHistory').evaluate('(node) => { node.closest("details").open = true; }')
                    await page.locator('#turnWorkHistory [data-answer-action="cancel"][data-answer-turn="' + cancelled_turn + '"]').click(timeout=15000)
                    await page.reload()
                    await page.locator('#authGate').wait_for(state='hidden')
                    await page.wait_for_function('id => state?.workLedger?.turnInbox.some(t => t.turnId === id && t.answer?.status === "cancelled")', arg=cancelled_turn, timeout=15000)
                    assert await page.locator('[data-turn-id="' + cancelled_turn + '"] .completed-answer').count() == 0
                    report['answerCancellationSurvivesReload'] = True
                # Exercise actual tab storage across reload, isolated from the
                # signed-in account so this check never creates cloud requests.
                await page.evaluate('''() => {
                    const outbox = new TurnOutbox(sessionStorage, 'storage-test', 'case-a');
                    const item = {type: 'control', clientRequestId: 'stable-id', transcript: 'pending question'};
                    outbox.add(item);
                    outbox.add(item);
                    if (outbox.entries().length !== 1) throw Error('duplicate local pending request');
                    if (new TurnOutbox(sessionStorage, 'other-user', 'case-a').entries().length) throw Error('cross-user pending request');
                    if (new TurnOutbox(sessionStorage, 'storage-test', 'other-case').entries().length) throw Error('cross-case pending request');
                }''')
                await page.reload()
                await page.locator('#authGate').wait_for(state='hidden')
                await page.wait_for_function('Boolean(state?.workLedger)')
                await page.evaluate('''() => {
                    const outbox = new TurnOutbox(sessionStorage, 'storage-test', 'case-a');
                    if (outbox.entries()[0]?.clientRequestId !== 'stable-id') throw Error('pending request lost on reload');
                    outbox.acknowledge('stable-id');
                    if (outbox.entries().length) throw Error('acknowledged request retained');
                    outbox.add({type: 'control', clientRequestId: 'delete-id', transcript: 'remove'});
                    TurnOutbox.clearUser(sessionStorage, 'storage-test');
                    if (outbox.entries().length) throw Error('deleted user request retained');
                }''')
                report['pendingTextSurvivesReload'] = True
                await page.locator('#turnWorkflowDetails > summary').click()
                for width in (1440, 390):
                    await page.set_viewport_size({'width': width, 'height': 1000})
                    await page.evaluate('scrollTo(0, 0)')
                    await page.screenshot(path=str(output / f'viewport-{width}.png'))
                    await page.screenshot(path=str(output / f'ui-{width}.png'), full_page=True)
                    layout = await page.evaluate('''() => ({width: innerWidth,
                        scrollWidth: document.documentElement.scrollWidth,
                        workflowVisible: document.querySelector('#liveWorkflow').getBoundingClientRect().height > 0})''')
                    report['viewports'].append(layout)
                await page.locator('#logout').click()
                await page.locator('#authGate').wait_for(state='visible')
                await page.locator('#loginMode').click()
                await page.locator('#authUsername').fill('browser-acceptance')
                await page.locator('#authPassword').fill('isolated-browser-account-123')
                await page.locator('#authSubmit').click()
                await page.locator('#authGate').wait_for(state='hidden')
                await page.wait_for_function("document.querySelector('#sessionSelect').selectedOptions[0]?.textContent.includes('browser-real-session')")
                report['sessionRestoredAfterLogin'] = True
                if real_turn:
                    await page.wait_for_function("document.querySelector('[data-performance=\"responseFirstAudioMs\"]').textContent.includes('ms')")
                    report['performanceRestoredAfterLogin'] = True
                report['passed'] = (not report['pageErrors'] and not report['failedRequests']
                                    and all(v['scrollWidth'] <= v['width'] and v['workflowVisible']
                                            for v in report['viewports']))
            finally:
                await browser.close()
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    (output / 'acceptance.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--real-turn', action='store_true')
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(verify(args.output, args.real_turn)) else 1)
