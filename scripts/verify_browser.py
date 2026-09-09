"""Exercise the production UI in installed Chrome/Edge with isolated data.

No mocked endpoints or media. This verifies UI/account/session flows, not hardware.
Requires playwright; does not download a browser.
"""
import argparse
import asyncio
import json
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
                await page.locator('#liveWorkflow').wait_for(state='visible')
                page.once('dialog', lambda dialog: dialog.accept('browser-real-session'))
                await page.locator('#newSession').click()
                await page.wait_for_function("document.querySelector('#sessionSelect').selectedOptions[0]?.textContent.includes('browser-real-session')")
                report['sessionCreated'] = True
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
                assert await page.locator('[data-performance="routerMs"]').text_content() == '-'
                if real_turn:
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
                        await page.locator('#insightKind').select_option(kind)
                        async with page.expect_response(lambda response: response.url.endswith('/api/insights') and response.request.method == 'POST', timeout=60000) as pending:
                            await page.locator('#insightGenerate').click()
                        response = await pending.value
                        result = await response.json()
                        report['insights'].append(result)
                        assert result['status'] == 'completed', result
                        assert result['coverage']['complete']
                        assert '张岚' in ''.join(item['text'] for item in result['summary']), result
                        assert '验收' in ''.join(item['text'] for item in result['summary']), result
                    async with page.expect_response(lambda response: response.url.endswith('/api/insights') and response.request.method == 'POST', timeout=60000) as pending:
                        await page.locator('#proactiveSuggestions').check()
                    suggestions = await (await pending.value).json()
                    report['automaticSuggestions'] = suggestions
                    assert suggestions['status'] == 'completed', suggestions
                    assert suggestions['suggestions'], suggestions
                    await page.locator('#proactiveSuggestions').uncheck()
                    await page.locator('#promptAuditDetails').evaluate('(node) => { node.open = true; }')
                    await page.wait_for_function("document.querySelector('#actualPromptRequests').textContent.includes('insight.request')")
                    report['realPromptAuditVisible'] = True
                for width in (1440, 390):
                    await page.set_viewport_size({'width': width, 'height': 1000})
                    await page.evaluate('scrollTo(0, 0)')
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
