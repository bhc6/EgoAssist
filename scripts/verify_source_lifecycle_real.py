"""One real provider request through native HTTP, followed by scoped erasure."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient, TestServer
from dotenv import load_dotenv
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.application.composition import create_app


async def verify(output):
    load_dotenv()
    output.mkdir(parents=True, exist_ok=False)
    runtime = output / 'runtime.json'
    report = {'passed': False, 'protocol': 'native-http-lifecycle-smoke', 'checks': {}}
    try:
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(runtime), 'unused', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            async def post(path, payload=None, expected=200):
                response = await client.post(path, json=payload)
                body = await response.json()
                assert response.status == expected, (path, response.status, body)
                return body
            auth = await post('/api/auth/register', {'username': 'lifecycle-check', 'password': 'Lifecycle-local-20260917'})
            first = (await post('/api/sessions', expected=201))['session']['id']
            question = 'What is my parcel pickup code?'
            await post('/api/history/import', {'conversation_id': first, 'events': [
                {'id': 'parcel', 'speaker': 'Alice', 'text': 'My parcel pickup code is 482731.',
                 'source_at': '2026-09-17T10:00:00+08:00', 'timezone': 'Asia/Shanghai'}]}, expected=201)
            packet = await post('/api/history/query', {'conversation_id': first, 'question': question})
            second = (await post('/api/sessions', expected=201))['session']['id']
            await post('/api/history/import', {'conversation_id': second, 'events': [
                {'id': 'bicycle', 'speaker': 'Bob', 'text': 'My bicycle is blue.', 'source_at': None}]}, expected=201)
            other = await post('/api/history/query', {'conversation_id': second, 'question': 'bicycle'})
            answer = await post('/api/history/ask', {'conversation_id': first, 'question': question,
                'evidence_snapshot_id': packet['snapshotId']})
            report['checks']['snapshotSurvivedNewSession'] = True
            report['providerRequestId'] = answer['result']['providerRequestId']
            report['usage'] = answer['result']['usage']
            report['validation'] = answer['result']['validation']
            assert '482731' in answer['result']['answer']
            report['checks']['literalPickupCodeAnswered'] = True
            await post('/api/history/delete', {'conversation_id': first, 'source_id': 'parcel'})
            restored = await post('/api/history/query', {'conversation_id': second, 'question': 'bicycle',
                'evidence_snapshot_id': other['snapshotId']})
            assert restored == other
            report['checks']['unrelatedSnapshotSurvivedDeletion'] = True
            await post('/api/history/query', {'conversation_id': first, 'question': question,
                'evidence_snapshot_id': packet['snapshotId']}, expected=400)
            account = output / 'users' / auth['user']['id']
            journal = account / 'sessions' / (first + '.jsonl')
            assert '482731' not in journal.read_text('utf-8')
            rows = [json.loads(line) for line in journal.read_text('utf-8').splitlines()]
            assert not any(row['type'] in {'evidence.answer.request', 'evidence.answer.response', 'evidence.package'} for row in rows)
            report['checks']['sourceAndModelAuditErased'] = True
            report['erasedTypes'] = sorted({row['originalType'] for row in rows if row['type'] == 'retention.deleted'})
            report['passed'] = all(report['checks'].values())
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        (output / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(verify(parser.parse_args().output.resolve()))
