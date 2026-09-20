"""Inspect current reports without changing product behavior or fabricating model output.

Synthetic journals test selection boundaries. UI checks render a persisted,
previously generated real report through the current app/API in an isolated case.
No fresh cloud generation or claims of real personal diary data are made.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.memory.insights import calendar_period, collect_sources
from egoassist.memory.temporal import ClockContext
from run_egoassist import create_app


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def probes(out):
    clock = ClockContext.current('Asia/Shanghai')
    records = []
    for day in range(1, 31):
        for index in range(100):
            stamp = f'2026-09-{day:02d}T10:{index//60:02d}:{index%60:02d}+08:00'
            records.append({'type': 'message', 'id': f'd{day}-{index}', 'status': 'completed',
                            'timestamp': stamp, 'message': {'role': 'user', 'content':
                            f'合成边界测试：第{day}天第{index}条记录，今天的工作进展和生活安排需要保留。'}})
    path = out/'synthetic-month.jsonl'
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in records), encoding='utf-8')
    selected = collect_sources([path], calendar_period('monthly', '2026-09-17', clock))
    monthly = {k: v for k, v in selected.items() if k != 'records'}
    monthly['includedDays'] = dict(Counter(r['recordedAt'][:10] for r in selected['records']))
    kinds = ['message', 'ambient_observation', 'answer.job', 'tool.result', 'memory.commit', 'insight.result']
    mixed = []
    for index, kind in enumerate(kinds):
        row = {'type': kind, 'id': str(index), 'timestamp': '2026-09-16T10:00:00+08:00',
               'status': 'completed', 'text': '合成来源类型探针'}
        if kind == 'message': row['message'] = {'role': 'user', 'content': row['text']}
        if kind == 'ambient_observation': row.update(status='accepted', evidence={
            'capturedAt': row['timestamp'], 'visibleEvidence': ['合成观察'], 'uncertainties': []})
        mixed.append(row)
    path = out/'synthetic-lanes.jsonl'
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in mixed), encoding='utf-8')
    selected = collect_sources([path], calendar_period('daily', '2026-09-16', clock))
    result = {'synthetic': True, 'monthlyBudget': monthly, 'laneProbe': {
        'inputTypes': kinds, 'selectedTypes': [kinds[int(r['eventId'])] for r in selected['records']]}}
    save(out/'selection-probes.json', result)
    return result


async def inspect(out):
    archive = ROOT/'outputs/insight-recovery-20260909-1/acceptance.json'
    prior = json.loads(archive.read_text('utf-8'))
    actual = next(r for r in prior['results'] if r['period']['kind'] == 'daily')
    assert prior['mock'] is False and actual['providerRequestId']
    report = {'selection': probes(out), 'ui': {'source': str(archive),
              'mode': 'archived real model report restored to isolated current app; no fresh model call',
              'originalProviderRequestId': actual['providerRequestId']}}
    runtime = out/'runtime'
    async with TestServer(create_app(str(BUILTIN_SKILLS_ROOT), None, str(runtime/'runtime.json'),
                                     'unused', 'Ethan')) as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge', headless=True)
            context = await browser.new_context(viewport={'width':1440,'height':1000})
            base = str(server.make_url('')).rstrip('/')
            auth_response = await context.request.post(base+'/api/auth/register', data={
                'username':'diary-audit', 'password':'isolated-diary-audit-2026'})
            assert auth_response.ok, await auth_response.text()
            uid = (await auth_response.json())['user']['id']
            response = await context.request.post(base+'/api/sessions', data={})
            sid = (await response.json())['session']['id']
            journal = SessionJournal(runtime/'users'/uid/'sessions'/f'{sid}.jsonl', session_id=sid)
            copied = {k:v for k,v in actual.items() if k not in {'type','id','parentId','timestamp'}}
            copied['auditRestoration'] = {'source':str(archive),'freshGeneration':False}
            restored = journal.append('insight.result', copied)
            for index in range(14):
                journal.append('insight.job', {'jobId':f'audit-job-{index}', 'status':'completed',
                    'phase':'finished','attempt':1,'createdAt':datetime.now(timezone.utc).isoformat(),
                    'specification':{'kind':'daily','anchor':'2026-09-09','scope':'account'},
                    'resultEventId':restored['id'], 'auditSyntheticIndexOnly':True})
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(base+'/#/reports')
            await page.wait_for_function('state?.workLedger && selectedSessionId')
            if await page.evaluate('selectedSessionId') != sid:
                await page.select_option('#sessionSelect', sid)
            await page.wait_for_function('document.querySelectorAll(".insight-item").length>0')
            report['ui']['renderedTitle'] = await page.locator('#insightResultTitle').inner_text()
            report['ui']['defaultScope'] = await page.locator('#insightScope').input_value()
            report['ui']['visibleJobsOf14'] = await page.locator('.insight-job').count()
            report['ui']['summaryItems'] = await page.locator('.insight-item').count()
            report['ui']['viewportChecks'] = []
            for width in (1440,390):
                await page.set_viewport_size({'width':width,'height':1000})
                await page.screenshot(path=str(out/f'current-reports-{width}.png'),full_page=True)
                report['ui']['viewportChecks'].append({'width':width,
                    'horizontalOverflow':await page.evaluate('document.documentElement.scrollWidth>innerWidth')})
            await page.set_viewport_size({'width':1440,'height':1000})
            await page.locator('.insight-item details summary').first.click()
            await page.screenshot(path=str(out/'current-reports-source.png'),full_page=True)
            await page.reload()
            await page.wait_for_function('document.querySelectorAll(".insight-item").length>0')
            report['ui']['reloadRetainsResult'] = await page.locator('#insightResultTitle').inner_text() == report['ui']['renderedTitle']
            other = await context.request.post(base+'/api/sessions', data={})
            other_sid = (await other.json())['session']['id']
            empty = await (await context.request.get(base+f'/api/insights?session={other_sid}')).json()
            report['ui']['newSessionSeesAccountReport'] = bool(empty['results'])
            old_by_id = await (await context.request.get(base+f'/api/insights?session={sid}&result={restored["id"]}')).json()
            report['ui']['persistedResultFetchableById'] = bool(old_by_id['results'])
            report['ui']['pageErrors'] = errors
            await browser.close()
    save(out/'audit.json', report)
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    asyncio.run(inspect(args.output.resolve()))
