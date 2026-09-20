"""Frozen EgoLife MCQ regression: current Timeline versus source evidence retrieval.

Uses actual production retrieval components and the unchanged frozen MCQ answer
contract. This is caption/transcript component evaluation, not realtime AV or
native HTTP end-to-end evaluation. Gold is read only after predictions persist.
"""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from compare_egolife_mcq import (Client, MODEL, PROMPT, SEEDS, JUDGE_PROMPT,
    answer_payload, evaluate, fingerprint, identity, read, target_metrics)
from egolife_independent_modes import absolute_seconds, clock_seconds, synthetic_time
from egoassist.core.journal import SessionJournal
from egoassist.core.source_events import history_event
from egoassist.eval.controlled_retrieval import digest, write_json
from egoassist.memory import TimelineMemory
from egoassist.memory.source_index import SourceIndex

ARMS = ('A', 'S')


def retrieve(args, queries, sources):
    old = {r['id']: r for r in read(args.previous / 'retrieval.json')}
    baseline_db = args.output / 'timeline-replay.sqlite3'
    original = sqlite3.connect((args.previous / 'timeline.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    copied = sqlite3.connect(baseline_db)
    try:
        original.backup(copied)
    finally:
        copied.close(); original.close()
    memory = TimelineMemory(baseline_db)
    rows = []
    old_limit = os.environ.get('EGOASSIST_MEMORY_CANDIDATE_LIMIT')
    os.environ['EGOASSIST_MEMORY_CANDIDATE_LIMIT'] = '256'
    try:
        for query in sorted(queries, key=lambda q: q['cutoff'], reverse=True):
            with memory._connect() as db:
                db.execute('DELETE FROM memory_cards WHERE recorded_at>?',
                    (synthetic_time(query['cutoff']).isoformat(timespec='milliseconds'),))
            started = time.perf_counter()
            base = memory.retrieve(query['question'], user_id='A1_JAKE', case_id='official-heldout',
                scope='case', limit=5, now=synthetic_time(query['cutoff']), include_hard_candidates=True)
            pool = [r['cardId'] for r in base['hardCandidates']]
            chosen = [r['cardId'] for r in base['selected']]
            assert set(pool) == set(old[query['id']]['candidateIds']), 'Timeline candidate pool changed'
            assert chosen == old[query['id']]['selectedIds'], 'Timeline baseline ranking changed'
            row = {'id': query['id'], 'arm': 'A', 'candidateIds': pool, 'selectedIds': chosen,
                'rankedIds': [r['cardId'] for r in base['hardCandidates']
                    if not (set(r['rejectionReasons']) - {'outside-top-k'}) and r['score'] >= base['minimumScore']],
                'retrievalMs': (time.perf_counter() - started) * 1000, 'baselineSelectionIdentical': True}
            rows.extend({**row, 'seed': seed} for seed in SEEDS)
    finally:
        if old_limit is None:
            os.environ.pop('EGOASSIST_MEMORY_CANDIDATE_LIMIT', None)
        else:
            os.environ['EGOASSIST_MEMORY_CANDIDATE_LIMIT'] = old_limit
    root = args.output / 'raw-evidence'
    if (root / 'sessions' / 'history.jsonl').exists():
        raise ValueError('Incomplete previous ingestion; use a new output directory')
    journal = SessionJournal.open_in(root / 'sessions', 'history')
    index = SourceIndex(root)
    ordered = sorted(sources.values(), key=lambda s: (s['end'], s['start'], s['sourceId']))
    imported, mapping, packets = 0, {}, []
    for qi, query in enumerate(sorted(queries, key=lambda q: q['cutoff']), 1):
        while imported < len(ordered) and ordered[imported]['end'] <= query['cutoff']:
            source = ordered[imported]
            # Dataset DAY offsets are not real calendar timestamps or identified speakers.
            source_value = history_event({'id': source['sourceId'], 'text': source['text'],
                'speaker': source['kind'], 'source_at': None, 'timezone': None})
            event = journal.append('source.event', {'source': source_value},
                idempotency_key='history:' + source['sourceId'])
            mapping[journal.session_id + ':' + event['id']] = source['sourceId']
            imported += 1
            if imported % 5000 == 0:
                print(json.dumps({'imported': imported, 'totalAvailable': len(ordered)}), flush=True)
        packet = index.search(query['question'], journal.session_id)
        packets.append({'id': query['id'], 'cutoff': query['cutoff'], 'packet': packet})
        selected = list(dict.fromkeys(s['externalId'] for s in packet['sources'] + packet['conversation']))
        candidates = list(dict.fromkeys(mapping[r['sourceId']] for r in packet['audit']))
        for source in packet['sources'] + packet['conversation']:
            assert source['text'] == sources[source['externalId']]['text'], 'Source excerpt contract changed'
        assert all(sources[sid]['end'] <= query['cutoff'] for sid in set(selected + candidates))
        assert packet['coverage']['sourceEvents'] == imported
        row = {'id': query['id'], 'arm': 'S', 'candidateIds': candidates, 'selectedIds': selected,
            'rankedIds': candidates, 'retrievalMs': packet['retrievalMs'], 'snapshotId': packet['snapshotId'],
            'coverage': packet['coverage'], 'visibleSources': imported}
        rows.extend({**row, 'seed': seed} for seed in SEEDS)
        write_json(args.output / 'source-packets.json', packets)
        print(json.dumps({'sourceQueries': qi, 'total': len(queries), 'visible': imported,
                          'selectedSources': len(selected)}), flush=True)
    write_json(args.output / 'retrieval.json', rows)
    return rows


def score(args, queries, sources, retrieval, predictions, client):
    # Read labels only after all prediction files have been written.
    gold = {r['id']: r for r in read(args.dataset / 'gold-private.json')}
    query_map = {q['id']: q for q in queries}
    indexed = {identity(r): r for r in retrieval}
    cache_path = args.output / 'judge-cache.json'
    cache = read(cache_path) if cache_path.exists() else {}
    rows = []
    for n, pred in enumerate(predictions, 1):
        retrieved, query = indexed[identity(pred)], query_map[pred['id']]
        label = gold[pred['id']]
        target = label['target']
        moment = absolute_seconds(target['date'], clock_seconds(target['time']))
        targets = {sid for sid, source in sources.items() if source['kind'] == 'caption'
            and source['end'] <= query['cutoff'] and source['start'] < moment + 1 and source['end'] >= moment}
        row = {**retrieved, **pred, 'goldChoice': label['answer'],
            'correct': pred['choice'] == label['answer'] if pred['status'] == 'completed' else None,
            'targetAnyEvidenceHit': bool(targets & set(retrieved['selectedIds'])),
            'targetCandidateHit': bool(targets & set(retrieved['candidateIds'])),
            'videoGroup': target['date'] + ':' + target['time'][:2], 'support': None}
        if pred['choice'] in ('A', 'B', 'C', 'D'):
            payload = {'question': query['question'], 'answer': query['options'][pred['choice']],
                       'evidence': answer_payload(query, retrieved, sources)['evidence']}
            key = digest([MODEL, JUDGE_PROMPT, payload])
            if not payload['evidence']:
                row['support'] = False
            else:
                try:
                    result = cache.get(key)
                    if result is None:
                        result = client.call('judge', JUDGE_PROMPT, payload, 17, identity(pred))
                    if not isinstance(result, dict) or type(result.get('supported')) is not bool:
                        raise ValueError('Invalid support judgment')
                    cache[key] = result
                    row.update(support=result['supported'], supportRationale=result.get('rationale'))
                    write_json(cache_path, cache)
                except Exception as exc:
                    row['judgeError'] = type(exc).__name__
        rows.append(row)
        write_json(args.output / 'scored.json', rows)
        if n % 30 == 0:
            print(json.dumps({'scored': n, 'planned': len(predictions)}), flush=True)
    archive = [r for r in read(args.baseline / 'scored.json') if r['arm'] == 'A']
    def aggregate(group):
        complete = all(r['status'] == 'completed' for r in group)
        judged = all(r.get('support') is not None for r in group)
        return {'planned': len(group), 'completed': sum(r['status'] == 'completed' for r in group),
            'correct': sum(r['correct'] is True for r in group),
            'accuracy': statistics.mean(r['correct'] for r in group) if complete else None,
            'support': statistics.mean(r['support'] for r in group) if complete and judged else None,
            'correctAndSupported': sum(r['correct'] is True and r.get('support') is True for r in group),
            'correctBySeed': {str(seed): sum(r['correct'] is True for r in group if r['seed'] == seed) for seed in SEEDS}}
    summary = {'previousTimeline': aggregate(archive), 'arms': {}, 'independentQuestions': len(queries),
        'claimBoundary': 'Exposed 30-question caption/transcript component regression. Different retrieval scopes and context budgets; unchanged MCQ answer contract. Not raw AV or full application QA.',
        'cost': None, 'standardEvidenceRecall': None, 'providerCalls': len(client.audit)}
    for arm in ARMS:
        group = [r for r in rows if r['arm'] == arm]
        unique = {r['id']: r for r in group}.values()
        calls = [c for c in client.audit if c['role'] == 'answer' and c['identity'].endswith(':' + arm)]
        summary['arms'][arm] = {**aggregate(group),
            'targetCandidateQuestions': sum(r['targetCandidateHit'] for r in unique),
            'targetFinalEvidenceQuestions': sum(r['targetAnyEvidenceHit'] for r in unique),
            'medianSelectedSources': statistics.median(len(r['selectedIds']) for r in unique),
            'medianEvidenceCharacters': statistics.median(sum(len(sources[sid]['text']) for sid in r['selectedIds']) for r in unique),
            'medianRetrievalMs': statistics.median(r['retrievalMs'] for r in unique),
            'answerInputTokens': sum((c.get('usage') or {}).get('input_tokens', 0) for c in calls),
            'answerOutputTokens': sum((c.get('usage') or {}).get('output_tokens', 0) for c in calls)}
    pairs = {(r['id'], r['seed']): r for r in rows if r['arm'] == 'A'}
    if all(r['status'] == 'completed' for r in rows):
        groups = defaultdict(list)
        for row in rows:
            if row['arm'] == 'S':
                groups[row['videoGroup']].append(int(row['correct']) - int(pairs[row['id'], row['seed']]['correct']))
        differences = [statistics.mean(values) for values in groups.values()]
        rng = random.Random(20260917)
        draws = sorted(statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(2000))
        summary['pairedVideoGroupDifference'] = {'groups': len(groups), 'mean': statistics.mean(differences),
            'bootstrap95': [draws[49], draws[1949]], 'unit': 'target day/hour; three seeds do not add independent questions'}
    summary['complete'] = len(rows) == 180 and all(r['status'] == 'completed' and r.get('support') is not None for r in rows)
    write_json(args.output / 'summary.json', summary)
    return summary


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    freeze = read(args.dataset / 'freeze.json')
    for name, expected in freeze['files'].items():
        assert fingerprint(args.dataset / name) == expected, 'Frozen dataset changed'
    plan = {'schema': 'egolife.source-evidence-regression.v1', 'dataset': str(args.dataset.resolve()),
        'baseline': str(args.baseline.resolve()), 'previous': str(args.previous.resolve()),
        'datasetHashes': freeze['files'], 'seeds': SEEDS, 'model': MODEL, 'answerPrompt': PROMPT,
        'judgePrompt': JUDGE_PROMPT, 'questionCount': 30, 'plannedAnswers': 180, 'arms': list(ARMS),
        'A': 'Current Timeline; budget256/top5, must match frozen baseline candidates and ranking',
        'S': 'Production SourceIndex; raw SourceEvent replay in source-end order; candidates256/anchors12/adjacent1/recent8/24000+8000 chars',
        'input': 'Official captions and transcripts; no raw media; source end <= question cutoff',
        'ingestion': 'Core validated source.event journal replay; not native import HTTP or semantic extraction',
        'gold': 'Scoring only, after frozen prediction file; not query, ranking, cutoff or prompt',
        'codeHashes': {str(p.relative_to(ROOT)): fingerprint(p) for p in (
            Path(__file__).resolve(), ROOT/'scripts/compare_egolife_mcq.py',
            ROOT/'egoassist/memory/source_index.py', ROOT/'egoassist/core/source_events.py',
            ROOT/'egoassist/memory/timeline.py', ROOT/'egoassist/memory/grounding.py')},
        'historicalScoredSha256': fingerprint(args.baseline / 'scored.json')}
    plan_path = args.output / 'plan.json'
    if plan_path.exists():
        assert read(plan_path) == plan, 'Plan/code changed; use a fresh output directory'
    else:
        write_json(plan_path, plan)
    queries = read(args.dataset / 'questions.json')
    assert len(queries) == 30
    sources = {s['sourceId']: s for s in read(args.dataset / 'sources.json')}
    client = Client(args.output)
    client.call('preflight', 'Return JSON {"ok":true}.', {'probe': 'availability'}, 17)
    path = args.output / 'retrieval.json'
    retrieval = read(path) if path.exists() else retrieve(args, queries, sources)
    expected = {(q['id'], seed, arm) for q in queries for seed in SEEDS for arm in ARMS}
    assert len(retrieval) == len(expected) and {(r['id'], r['seed'], r['arm']) for r in retrieval} == expected
    predictions = evaluate(args, queries, sources, retrieval, client)
    frozen_predictions = fingerprint(args.output / 'predictions.json')
    summary = score(args, queries, sources, retrieval, predictions, client)
    assert fingerprint(args.output / 'predictions.json') == frozen_predictions
    write_json(args.output / 'prediction-freeze.json', {'sha256': frozen_predictions, 'unchangedAfterScoring': True})
    print(json.dumps(summary, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT/'artifacts/egolife-independent-freeze-20260914-r4')
    parser.add_argument('--previous', type=Path, default=ROOT/'artifacts/egolife-independent-modes-20260914-r2')
    parser.add_argument('--baseline', type=Path, default=ROOT/'artifacts/egolife-abcd-mcq-20260914-r2')
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())
