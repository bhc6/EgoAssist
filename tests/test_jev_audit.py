import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess

from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.realtime.prompt_audit import decision_audit
from egoassist.realtime.work_ledger import CaseWorkLedger
from run_egoassist import create_app


def decision():
    stage = {'model': 'typesafe/jev-test', 'resolvedModel': 'typesafe/jev-test-revision',
        'promptVersion': 'archived-prompt', 'latencyMs': 400, 'httpStatus': 200,
        'route': 'recall', 'routeOverride': 'evidence-scope-recall', 'request': {
            'state': 'private-large-context', 'questions': {
                'route': {'instructions': '实际保存的判断题 <script>bad()</script>',
                    'criteria': {'reply': '直接回答', 'recall': '查询历史', 'other': '旁听', 'stop': '停止'}},
                'target': {'instructions': '操作哪个已有答案？', 'criteria': {'none': '无目标', 't1': '当时的问题'}}}},
        'answers': {'route': {'choice': 'reply', 'probabilities': {'reply': .9, 'recall': .09, 'other': .01, 'stop': 0}},
                    'target': {'choice': 'none', 'probabilities': {'none': 1, 't1': 0}}},
        'rawResponse': 'large-response', 'structuredRawOutput': 'large-parsing-output'}
    return {'status': 'classified', 'model': 'structured-model', 'routerBackend': 'openrouter-decisions',
        'modelsUsed': ['typesafe/jev-test-revision', 'structured-model'], 'latencyMs': 1500,
        'rawOutput': 'large-output', 'memoryQuery': 'referential', 'evidenceScope': 'temporal-history',
        'interaction': {'audience': 'assistant', 'action': 'reply', 'output': 'speech'},
        'requestMetrics': {'decisions': stage, 'structuredReason': 'temporal-history-evidence',
            'cancellationCheck': {'latencyMs': 120, 'request': {'state': 'private-review-context',
                'questions': {'verdict': {'instructions': '是否撤销？', 'criteria': {'yes': '同意', 'no': '不同意'}}}},
                'answers': {'verdict': {'choice': 'no', 'probabilities': {'yes': 0, 'no': 1}}}}}}


def test_detail_preserves_actual_questions_and_reviews_without_full_context():
    row = {'type': 'control_decision', 'decision': decision()}
    original = deepcopy(row)
    detail = decision_audit(row)
    assert row == original
    metrics = detail['decision']['requestMetrics']
    assert metrics['decisions']['questions'] == row['decision']['requestMetrics']['decisions']['request']['questions']
    assert metrics['cancellationCheck']['questions']['verdict']['instructions'] == '是否撤销？'
    encoded = json.dumps(detail)
    for removed in ('private-large-context', 'private-review-context', 'large-response', 'large-parsing-output', 'large-output'):
        assert removed not in encoded


def test_detail_endpoint_is_scoped_and_ledger_stays_compact(tmp_path):
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / 'runtime.json'), 'test', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/api/prompts?event=unknown')).status == 401
            user = (await (await client.post('/api/auth/register', json={'username': 'jev-owner', 'password': 'valid-password-123'})).json())['user']
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            journal = SessionJournal(tmp_path / 'users' / user['id'] / 'sessions' / (sid + '.jsonl'), session_id=sid)
            ledger = CaseWorkLedger(journal)
            turn = ledger.capture_turn('查询历史', source='test')
            row = journal.append('control_decision', {'turnId': turn['turnId'], 'decision': decision()})
            visible = ledger.snapshot()['turnInbox'][-1]['semantic']
            assert visible['auditEventId'] == row['id']
            assert 'requestMetrics' not in visible and 'rawOutput' not in visible
            response = await client.get('/api/prompts', params={'session': sid, 'event': row['id']})
            assert response.status == 200 and response.headers['Cache-Control'] == 'private, no-store'
            data = await response.json()
            assert data['entry']['decision']['requestMetrics']['decisions']['questions']['target']['criteria']['t1'] == '当时的问题'
            assert 'templates' not in data and 'requests' not in data
            assert (await client.get('/api/prompts', params={'session': sid, 'event': turn['id']})).status == 404
            other_sid = (await (await client.post('/api/sessions')).json())['session']['id']
            assert (await client.get('/api/prompts', params={'session': other_sid, 'event': row['id']})).status == 404
            await client.post('/api/auth/logout')
            await client.post('/api/auth/register', json={'username': 'jev-other', 'password': 'valid-password-123'})
            assert (await client.get('/api/prompts', params={'session': sid, 'event': row['id']})).status in {400, 403, 404}
    asyncio.run(run())


def test_render_all_choices_including_zero_failures_and_distinct_timings():
    source = Path('egoassist/console/model_stages.js').read_text(encoding='utf8') + '\n' + Path('egoassist/console/jev_audit.js').read_text(encoding='utf8')
    data = decision_audit({'type': 'control_decision', 'decision': decision()})['decision']
    script = "const assert = require('node:assert/strict'); const esc = v => String(v ?? '').replaceAll('<', '&lt;').replaceAll('>', '&gt;');\n" + source
    script += '\nconst semantic = ' + json.dumps(data) + ';\n' + r'''
const html = jevAuditHTML(semantic);
for (const word of ['90%', '9%', '1%', '0%', '当时的问题', '是否撤销？', '120 ms', '400 ms', '1500 ms', 'archived-prompt']) assert.ok(html.includes(word), word);
assert.ok(html.includes('&lt;script&gt;bad()&lt;/script&gt;'));
assert.ok(!html.includes('<script>bad()'));
assert.ok(html.includes('模型原始路由') && html.includes('路由调整原因'));
assert.ok(html.includes('模型原始路由</b><span>reply'));
assert.ok(html.includes('最终路由结果</b><span>查找记忆'));
assert.ok(html.includes('取消答案独立复核'));
const failed = structuredClone(semantic);
failed.status = 'unavailable'; failed.error = 'TimeoutError';
delete failed.requestMetrics.decisions.answers;
const failureHTML = jevAuditHTML(failed);
assert.ok(failureHTML.includes('未形成有效路由') && failureHTML.includes('TimeoutError'));
assert.ok(failureHTML.includes('当时的问题') && failureHTML.includes('未返回'));
assert.ok(!failureHTML.includes('模型原始路由</b><span>reply'));
const legacy = jevAuditHTML({status:'classified', jevChoices:{route:{choice:'reply', probabilities:{reply:1, stop:0}}}});
assert.ok(legacy.includes('历史日志未记录选项说明') && legacy.includes('0%'));
assert.ok(!jevRouteSummary({memoryKind:'event', memoryText:'事实'}).includes('记忆检索'));
assert.ok(jevRouteDetails(semantic).includes('分类总耗时：1500 ms'));
'''
    subprocess.run(['node', '-'], input=script, text=True, encoding='utf8', check=True, timeout=15)
