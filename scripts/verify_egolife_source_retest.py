"""Read-only audit and human-readable comparison of the EgoLife evidence retest."""
import argparse
import json
from pathlib import Path
import sqlite3
import statistics

from compare_egolife_mcq import answer_payload, fingerprint, identity, read
from egolife_independent_modes import absolute_seconds, clock_seconds


def verify(output):
    plan = read(output / 'plan.json')
    dataset = Path(plan['dataset'])
    for name, expected in plan['datasetHashes'].items():
        assert fingerprint(dataset / name) == expected
    queries = {q['id']: q for q in read(dataset / 'questions.json')}
    sources = {s['sourceId']: s for s in read(dataset / 'sources.json')}
    labels = {g['id']: g for g in read(dataset / 'gold-private.json')}
    retrieval = {identity(r): r for r in read(output / 'retrieval.json')}
    predictions = {identity(r): r for r in read(output / 'predictions.json')}
    scored = {identity(r): r for r in read(output / 'scored.json')}
    expected = {f'{seed}:{qid}:{arm}' for qid in queries for seed in plan['seeds'] for arm in plan['arms']}
    assert set(retrieval) == set(predictions) == set(scored) == expected
    assert fingerprint(output / 'predictions.json') == read(output / 'prediction-freeze.json')['sha256']
    baseline = {r['id']: r for r in read(Path(plan['previous']) / 'retrieval.json')}
    packets = read(output / 'source-packets.json')
    assert len(packets) == len(queries) and {r['id'] for r in packets} == set(queries)
    db = sqlite3.connect((output / 'raw-evidence/source-index.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    try:
        mapping = dict(db.execute('SELECT id,json_extract(body,?) FROM events', ('$.externalId',)))
    finally:
        db.close()
    for record in packets:
        packet, query = record['packet'], queries[record['id']]
        assert packet['question'] == query['question'] and record['cutoff'] == query['cutoff']
        assert packet['coverage']['sourceEvents'] == sum(s['end'] <= query['cutoff'] for s in sources.values())
        selected = list(dict.fromkeys(s['externalId'] for s in packet['sources'] + packet['conversation']))
        candidates = list(dict.fromkeys(mapping[r['sourceId']] for r in packet['audit']))
        for source in packet['sources'] + packet['conversation']:
            assert source['text'] == sources[source['externalId']]['text']
            assert source['span'] == [0, len(source['text'])]
        for seed in plan['seeds']:
            row = retrieval[f'{seed}:{record["id"]}:S']
            assert row['selectedIds'] == selected and row['candidateIds'] == candidates
    for key, row in retrieval.items():
        query = queries[row['id']]
        assert all(sources[sid]['end'] <= query['cutoff'] for sid in set(row['candidateIds'] + row['selectedIds']))
        if row['arm'] == 'A':
            assert row['selectedIds'] == baseline[row['id']]['selectedIds']
            assert set(row['candidateIds']) == set(baseline[row['id']]['candidateIds'])
        pred = predictions[key]
        assert scored[key]['choice'] == pred['choice']
        assert scored[key]['correct'] == (pred['choice'] == labels[row['id']]['answer'] if pred['status'] == 'completed' else None)
    calls = read(output / 'calls.json')
    for call in calls:
        payload = json.loads(call['request']['input']['messages'][1]['content'])
        if call['role'] == 'answer':
            row = retrieval[call['identity']]
            assert payload == answer_payload(queries[row['id']], row, sources)
            assert call['request']['input']['messages'][0]['content'] == plan['answerPrompt']
            assert call['request']['model'] == plan['model']
            assert set(payload) == {'currentTime', 'question', 'options', 'evidence'}
        elif call['role'] == 'judge':
            assert set(payload) == {'question', 'answer', 'evidence'}
    summary = read(output / 'summary.json')
    for arm in plan['arms']:
        rows = [r for r in scored.values() if r['arm'] == arm]
        stats = summary['arms'][arm]
        assert stats['planned'] == len(rows)
        assert stats['correct'] == sum(r['correct'] is True for r in rows)
        if all(r['status'] == 'completed' for r in rows):
            assert stats['accuracy'] == statistics.mean(r['correct'] for r in rows)
        else:
            assert stats['accuracy'] is None
        if all(r['support'] is not None for r in rows):
            assert stats['support'] == statistics.mean(r['support'] for r in rows)
        unique = {r['id']: r for r in rows}.values()
        actual_hits = 0
        for row in unique:
            target = labels[row['id']]['target']
            moment = absolute_seconds(target['date'], clock_seconds(target['time']))
            query = queries[row['id']]
            target_ids = {sid for sid, s in sources.items() if s['kind'] == 'caption'
                and s['end'] <= query['cutoff'] and s['start'] < moment + 1 and s['end'] >= moment}
            actual_hits += bool(target_ids & set(row['selectedIds']))
        assert stats['targetFinalEvidenceQuestions'] == actual_hits
    result = {'verified': True, 'questions': len(queries), 'predictions': len(predictions),
        'baselineCandidatesAndSelectionIdentical': True, 'futureSourcesExcluded': True,
        'modelRequestPayloadsChecked': len(calls), 'goldFieldsExcludedFromRequests': True,
        'predictionsUnchangedAfterScoring': True, 'complete': summary['complete'], 'modelCallsByVerifier': 0}
    before_fix = output.parent / 'egolife-source-retest-20260917-r1' / 'source-packets.json'
    if before_fix.exists():
        fixed_packets = {row['id']: row['packet'] for row in packets}
        previous_packets = read(before_fix)
        for row in previous_packets:
            before, after = row['packet'], fixed_packets[row['id']]
            projection = lambda packet: [(s['externalId'], s['text'], s['span'])
                for s in packet['sources'] + packet['conversation']]
            assert projection(before) == projection(after)
            assert before['coverage'] == after['coverage']
            audit = lambda packet: [(r['score'], r['span'], r['selected'], r['reason']) for r in packet['audit']]
            assert audit(before) == audit(after)
        result['indexOptimizationRegression'] = {'queries': len(previous_packets),
            'evidenceContentOrderScoresUnchanged': True, 'originalPacketsSha256': fingerprint(before_fix)}
    (output / 'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    def percentage(value):
        return 'not measured' if value is None else f'{value * 100:.2f}%'
    lines = ['# EgoLife Evidence Regression', '',
        '30 exposed official questions, seeds 17/43/91, same qwen-flash forced-choice answer contract.',
        'S uses the production raw-source retrieval component with a larger context budget; A uses Timeline top-5.',
        'Caption/transcript evaluation only. No raw camera/audio or end-to-end product quality claim.', '',
        '| Run | Correct | Accuracy | LLM support | Correct and supported |',
        '| --- | ---: | ---: | ---: | ---: |']
    for name, stats in [('Previous A', summary['previousTimeline']), ('Current A', summary['arms']['A']), ('Raw source S', summary['arms']['S'])]:
        lines.append(f'| {name} | {stats["correct"]}/{stats["planned"]} | {percentage(stats["accuracy"])} | {percentage(stats["support"])} | {stats["correctAndSupported"]}/{stats["planned"]} |')
    lines += ['', 'The target-time metric is an overlap proxy, not official evidence recall. Support is an unreviewed model judgment.', '',
        '| Metric | Current A | Raw source S |', '| --- | ---: | ---: |']
    for key in ('targetCandidateQuestions', 'targetFinalEvidenceQuestions', 'medianSelectedSources',
                'medianEvidenceCharacters', 'medianRetrievalMs', 'answerInputTokens', 'answerOutputTokens'):
        lines.append(f'| {key} | {summary["arms"]["A"][key]} | {summary["arms"]["S"][key]} |')
    paired = summary.get('pairedVideoGroupDifference')
    if paired:
        lines += ['', f'Paired day/hour group difference: {percentage(paired["mean"])}; bootstrap 95% interval: '
            f'[{percentage(paired["bootstrap95"][0])}, {percentage(paired["bootstrap95"][1])}]. '
            f'{paired["groups"]} groups. Three seeds do not turn 30 questions into 90 independent questions.']
    archived = {identity(r): r for r in read(Path(plan['baseline']) / 'scored.json')}
    lines += ['', '## Answers Versus Ground Truth', '', 'Choices appear in seed order 17/43/91.', '',
        '| ID | Question | Ground truth | Previous A | Current A | Raw source S |', '| --- | --- | --- | --- | --- | --- |']
    for qid, query in queries.items():
        gold = labels[qid]['answer']
        choices = lambda rows, arm: '/'.join(rows[f'{seed}:{qid}:{arm}']['choice'] or '?' for seed in plan['seeds'])
        lines.append(f'| {qid} | {query["question"].replace("|", "/")} | {gold}: {query["options"][gold]} | '
                     f'{choices(archived, "A")} | {choices(scored, "A")} | {choices(scored, "S")} |')
    lines += ['', '[Plan](plan.json) | [Summary](summary.json) | [Model requests](calls.json) | [Evidence packets](source-packets.json) | [Verification](verification.json)']
    (output / 'comparison.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    verify(parser.parse_args().output)
