"""Frozen development retrieval probe and real native-answer diagnostics, never gold-routed scoring."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.application.composition import create_app
from egoassist.core.journal import SessionJournal
from egoassist.core.source_events import fingerprint
from egoassist.memory.source_index import SourceIndex
from egoassist.product.answer_service import AnswerService
from egoassist.eval.source_evidence import score_packet, retrieval_metrics, acceptance, gold_references


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def turns(sample):
    conversation = sample['conversation']
    result = []
    for key in sorted((k for k in conversation if k.startswith('session_') and not k.endswith('_date_time')),
                      key=lambda key: int(key.split('_')[1])):
        stamp = datetime.strptime(conversation[key+'_date_time'], '%I:%M %p on %d %B, %Y').isoformat()
        for row in conversation[key]:
            text = row['text']
            if row.get('blip_caption'):
                text += '\n[Dataset image caption, not a new visual observation: ' + row['blip_caption'] + ']'
            result.append({'id': row['dia_id'], 'text': text, 'speaker': row['speaker'],
                           'source_at': stamp, 'timezone': None})
    return result


async def main(folder, dataset):
    import os
    load_dotenv()
    folder.mkdir(parents=True, exist_ok=False)
    rows = json.loads(dataset.read_text('utf-8'))
    development = rows[:2]
    manifest = {'datasetSha256': hashlib.sha256(dataset.read_bytes()).hexdigest(),
        'developmentConversations': [r['sample_id'] for r in development],
        'reservedConversations': [r['sample_id'] for r in rows[2:]],
        'boundary': 'Previously evaluated dataset. Reserved conversations are not accessed in this iteration; not a new unseen test set.',
        'nativeQuestionsPerConversation': [0, 1, 2], 'diagnosticQuestionPerConversation': 0,
        'retrievalConfig': {'method': 'FTS5 porter + CJK bigrams', 'candidateLimit': 256, 'anchors': 12, 'characters': 24000},
        'goldUsage': 'Only post-retrieval scoring and explicitly separate offline oracle diagnostics',
        'protocol': 'native-history-v1; results are not comparable as a same-protocol model-only improvement'}
    save(folder/'protocol.json', manifest)
    runtime = folder/'runtime'/'runtime.json'
    runtime.parent.mkdir()
    report = {'passed': False, 'imports': [], 'retrieval': [], 'answers': [], 'diagnostics': []}
    service = AnswerService(os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY') or '', timeout=60)
    try:
        async with TestClient(TestServer(create_app(str(BUILTIN_SKILLS_ROOT), None, str(runtime), 'unused', 'Ethan'))) as client:
            auth = await (await client.post('/api/auth/register', json={'username': 'evidence-dev', 'password': 'Evidence-local-demo-20260917'})).json()
            uid = auth['user']['id']
            user_root = runtime.parent/'users'/uid
            for sample in development:
                sid = (await (await client.post('/api/sessions')).json())['session']['id']
                events = turns(sample)
                receipts = []
                for offset in range(0, len(events), 400):
                    response = await client.post('/api/history/import', json={'conversation_id': sid, 'events': events[offset:offset+400]})
                    receipt = await response.json(); assert response.status == 201, receipt
                    receipts.extend(receipt['events'])
                assert len(receipts) == len(events) and all(r['indexed'] for r in receipts)
                report['imports'].append({'conversation': sample['sample_id'], 'sessionId': sid,
                    'sourceTurns': len(events), 'indexedTurns': len(receipts), 'extraction': 'not-requested'})
                save(folder/(sample['sample_id']+'-imports.json'), receipts)
                packets = []
                for number, qa in enumerate(sample['qa']):
                    packet = await (await client.post('/api/history/query', json={'conversation_id': sid, 'question': qa['question']})).json()
                    assert 'snapshotId' in packet, packet
                    packets.append(packet)
                    report['retrieval'].append(score_packet(qa, packet, receipts,
                        conversation=sample['sample_id'], index=number))
                save(folder/(sample['sample_id']+'-retrieval-packets.json'), packets)
                print(sample['sample_id'] + ': imported ' + str(len(events)) + ', queried ' + str(len(packets)), flush=True)
                for number in manifest['nativeQuestionsPerConversation']:
                    qa, packet = sample['qa'][number], packets[number]
                    response = await client.post('/api/history/ask', json={'conversation_id': sid, 'question': qa['question'], 'evidence_snapshot_id': packet['snapshotId']})
                    result = await response.json()
                    row = {'conversation': sample['sample_id'], 'index': number, 'question': qa['question'],
                        'groundTruth': qa.get('answer'), 'goldEvidence': qa.get('evidence'), 'httpStatus': response.status, **result}
                    report['answers'].append(row); save(folder/'progress.json', report)
                    print(sample['sample_id'] + ':' + str(number) + ' ' + result.get('result', {}).get('status', 'error'), flush=True)
                qa = sample['qa'][0]
                all_sources = SourceIndex(user_root).events(sid)
                for protocol in ['oracle-offline-diagnostic', 'full-source-context-diagnostic']:
                    selected = all_sources if protocol.startswith('full') else [r for r in all_sources if r['externalId'] in gold_references(qa['evidence'])]
                    packet = {'schema': 'egoassist.evidence-package.v1', 'question': qa['question'], 'sessionId': sid,
                        'sources': selected, 'conversation': [], 'coverage': {'protocol': protocol, 'sourceEvents': len(selected)},
                        'conflicts': [], 'conflictStatus': 'not-adjudicated', 'missingInformation': [], 'status': 'diagnostic'}
                    packet['snapshotId'] = fingerprint(packet)
                    diagnostic_journal = SessionJournal(folder/(sample['sample_id']+'-'+protocol+'.jsonl'))
                    result = await service.answer(diagnostic_journal, qa['question'], packet)
                    report['diagnostics'].append({'protocol': protocol, 'excludedFromFormalScore': True,
                        'conversation': sample['sample_id'], 'question': qa['question'], 'groundTruth': qa['answer'], 'result': result})
                    save(folder/'progress.json', report)
            report['metrics'] = retrieval_metrics(report['retrieval'])
            report.update(acceptance(report['answers'], report['metrics']))
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        await service.close()
        save(folder/'verification.json', report)
    print(json.dumps({k:v for k,v in report.items() if k not in {'answers','diagnostics','retrieval'}}, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, default=Path('D:/DEMO/locomo-evaluation-20260917/upstream/data/locomo10.json'))
    args = parser.parse_args()
    asyncio.run(main(args.output.resolve(), args.dataset.resolve()))
