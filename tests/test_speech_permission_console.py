import asyncio
import json
from pathlib import Path
import subprocess

from aiohttp.test_utils import TestClient, TestServer
from egoassist.realtime.interaction import InteractionState
from egoassist.realtime.work_ledger import CaseWorkLedger
from test_ambient_console import ambient_app, register_client
from tests.support import run_node


def test_resume_speech_endpoint_is_scoped_guarded_and_durable(tmp_path, monkeypatch):
    original_init = InteractionState.__init__
    def seeded(self, journal):
        original_init(self, journal)
        if not self.permission:
            CaseWorkLedger(journal).capture_turn('先别说话', source='test', turn_id='quiet', sequence=1)
            self.apply({'audience': 'assistant', 'action': 'listen', 'output': 'silent'}, 'quiet')
            self.pause()
    monkeypatch.setattr(InteractionState, '__init__', seeded)
    async def run():
        async with TestClient(TestServer(ambient_app(tmp_path, monkeypatch))) as client:
            assert (await client.post('/api/interaction/resume-speech', json={})).status == 401
            await register_client(client, 'speech-permission')
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            state = await (await client.get('/api/state', params={'session': sid})).json()
            permission = state['interaction']; event_id = permission['permissionSource']['eventId']
            assert permission['quiet'] and permission['permissionSource']['text'] == '先别说话'
            assert (await client.post('/api/interaction/resume-speech', json={'sessionId': sid})).status == 400
            assert (await client.post('/api/interaction/resume-speech', json={'sessionId': sid, 'permissionId': 'old'})).status == 409
            response = await client.post('/api/interaction/resume-speech', json={'sessionId': sid, 'permissionId': event_id})
            assert response.status == 200
            result = (await response.json())['state']
            assert not result['interaction']['quiet'] and result['interaction']['paused']
            assert not result['ambient']['consented']  # Release grants neither camera access nor observation consent.
            assert not result['workLedger']['turnInbox'][0].get('answer')
            refreshed = await (await client.get('/api/state', params={'session': sid})).json()
            assert not refreshed['interaction']['quiet']
            assert (await client.post('/api/interaction/resume-speech', json={'sessionId': sid, 'permissionId': event_id})).status == 409
            async with TestClient(client.server) as other:
                await register_client(other, 'another-speaker')
                denied = await other.post('/api/interaction/resume-speech', json={'sessionId': sid,
                    'permissionId': result['interaction']['permissionSource']['eventId']})
                assert denied.status == 403
    asyncio.run(run())


def test_permission_copy_distinguishes_current_and_recorded_block():
    source = Path('egoassist/console/speech_permission.js').read_text(encoding='utf8')
    script = source + '''
console.log(JSON.stringify([
  speechPermissionText({quiet:true,allowReplies:true,permissionSource:{text:'上个厕所。'}}),
  observationPermissionText({reason:'output-not-permitted',outputPermission:{quiet:true}}, {quiet:false,paused:false}),
  speechPermissionText({quiet:false,paused:true}),
  observationPermissionText({reason:'stale-frame'}, {quiet:true}),
]));'''
    result = run_node(script, via_stdin=False)
    values = json.loads(result.stdout)
    assert '上个厕所。' in values[0] and '未设置到期' in values[0]
    assert '当时' in values[1] and '已解除' in values[1] and '不会自动补播' in values[1]
    assert '等待话语处理' in values[2] and values[3] == ''
