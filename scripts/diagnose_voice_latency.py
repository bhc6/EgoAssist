"""Correlate recorded input-to-audio latency and replay usage attribution offline."""
import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from egoassist.core.journal import SessionJournal
from egoassist.realtime.context_harness import ContextHarness, ContextLayer


def elapsed(start, end):
    return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000, 1)


def diagnose(source, output):
    output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    inputs = {row['turnId']: row for row in rows if row['type'] == 'turn_inbox'}
    result = {'source': str(source.resolve()), 'timingBoundary': 'server input acceptance to first audio event; not physical speaker playback', 'turns': []}
    for index, row in enumerate(rows):
        if row.get('eventType') != 'response.created':
            continue
        turn_id = row.get('payload', {}).get('origin', {}).get('turnId')
        if turn_id not in inputs:
            continue
        start = inputs[turn_id]
        response_id = row['responseId']
        follow = [entry for entry in rows[index:] if entry.get('responseId') == response_id]
        audio = next((entry for entry in follow if entry.get('eventType') == 'response.first_audio'), None)
        if audio is None:
            continue
        text = next((entry for entry in follow if entry.get('eventType') == 'response.first_transcript'), None)
        phases = [{k: entry[k] for k in ('timestamp', 'phase')} for entry in rows
                  if entry['type'] == 'turn.progress' and entry.get('turnId') == turn_id]
        metrics = [entry['payload'] for entry in rows if entry.get('eventType') == 'provider.pipeline.metrics'
                   and entry.get('payload', {}).get('responseId') == response_id]
        result['turns'].append({'turnId': turn_id, 'question': start['text'], 'inputChannel': start['source'],
            'inputAt': start['timestamp'], 'responseId': response_id, 'phases': phases,
            'inputToCreatedMs': elapsed(start['timestamp'], row['timestamp']),
            'inputToTextMs': elapsed(start['timestamp'], text['timestamp']) if text else None,
            'textToAudioMs': elapsed(text['timestamp'], audio['timestamp']) if text else None,
            'inputToAudioMs': elapsed(start['timestamp'], audio['timestamp']), 'metrics': metrics})
    # Freeze immediately before the first incorrectly attributed HTTP usage
    # triggers the observed Omni compaction. Keep the source journal untouched.
    bad = next((i for i, row in enumerate(rows) if row['type'] == 'provider_usage'
        and str(row.get('responseId', '')).startswith('modular-')
        and row.get('usage', {}).get('inputTokens', 0) >= 28672), None)
    if bad is not None:
        frozen = output / 'usage-boundary.jsonl'
        frozen.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows[:bad+1]), encoding='utf-8')
        journal = SessionJournal(frozen)
        harness = ContextHarness(journal)
        before = len(journal.entries(types={'compaction'}))
        built = harness.build([ContextLayer('policy', 'Use only supplied evidence.', required=True)])
        result['usageScopeReplay'] = {'realRecordedUsage': rows[bad],
            'oldRuleWouldForceCompaction': True,
            'newCompactions': len(journal.entries(types={'compaction'})) - before,
            'providerUsageAppliesToProjection': built['manifest']['providerUsageAppliesToProjection'],
            'offlineReplayOnly': True}
        assert result['usageScopeReplay']['newCompactions'] == 0
    (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'turns': len(result['turns']), 'report': str(output / 'report.json')}, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    diagnose(args.journal, args.output)
