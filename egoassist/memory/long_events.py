"""Rebuildable, source-bound event tracking; never executes user actions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import time
from uuid import uuid4

from egoassist.product.answer_service import AnswerService
from egoassist.prompts import prompt_text


def source(row):
    kind = row.get('type')
    if kind == 'ambient_observation' and row.get('status') == 'accepted':
        evidence = row.get('evidence') or {}
        if not evidence.get('visibleEvidence'):
            return None
        value = {'kind': 'sampled-observation', 'text': '；'.join(evidence['visibleEvidence']),
                 'at': evidence.get('capturedAt'), 'media': evidence.get('media'),
                 'uncertainties': evidence.get('uncertainties', [])}
    elif kind == 'message' and row.get('status') == 'completed' and row.get('message', {}).get('role') == 'user':
        value = {'kind': 'user-statement', 'text': row['message']['content'],
                 'at': (row.get('metadata') or {}).get('sourceRecordedAt') or row['timestamp'],
                 'turnId': row.get('turnId') or (row.get('metadata') or {}).get('turnId'),
                 'attribution': 'statement-not-proof-of-execution'}
    else:
        return None
    try:
        if datetime.fromisoformat(value['at']).tzinfo is None:
            return None
    except (TypeError, ValueError):
        return None
    return {**value, 'sourceId': row['id'], 'recordedAt': row['timestamp']}


def projection(journal):
    def reduce(state, row):
        evidence = source(row)
        if evidence:
            state['sources'][row['id']] = evidence
        if row.get('type') == 'long_event.reset':
            state.update(events={}, processed=set(), revision=row['id'], failures=0)
        if row.get('type') == 'long_event.batch':
            state['processed'].update(row['processedSourceIds'])
            state['events'].update({e['eventId']: e for e in row['events']})
            state['revision'] = row['id']
        if row.get('type') == 'long_event.job':
            state['job'] = row
            if row['status'] == 'failed': state['failures'] += 1
            elif row['status'] == 'completed': state['failures'] = 0
    return journal.project('long-events-v1', lambda: {'sources': {}, 'events': {}, 'processed': set(), 'revision': None, 'job': None, 'failures': 0}, reduce)


def pending(state):
    return sorted((s for s in state['sources'].values() if s['sourceId'] not in state['processed']),
                  key=lambda s: (datetime.fromisoformat(s['at']), s['sourceId']))


def next_batch(state):
    rows = pending(state)[:24]
    for index in range(1, len(rows)):
        if (datetime.fromisoformat(rows[index]['at']) - datetime.fromisoformat(rows[index-1]['at'])).total_seconds() > 1800:
            return rows[:index]
    return rows


def candidate_events(state, batch):
    """Offer open projects across days without merging separate visual occurrences.

    Time admits candidates, never proves identity. The model receives their raw
    evidence and must establish continuity from the new source statements.
    """
    start, end = (datetime.fromisoformat(batch[i]['at']) for i in (0, -1))
    statements = any(s['kind'] == 'user-statement' for s in batch)
    nearby, ongoing = [], []
    for event in state['events'].values():
        if any(ref not in state['sources'] for ref in event['sourceIds']):
            continue
        if datetime.fromisoformat(event['coverage']['firstEvidenceAt']) > end:
            continue
        gap = (start - datetime.fromisoformat(event['coverage']['lastEvidenceAt'])).total_seconds()
        if gap <= 1800:
            nearby.append(event)
        elif statements and event['status'] != 'completed' and gap <= 30 * 86400:
            ongoing.append(event)
    key = lambda e: datetime.fromisoformat(e['coverage']['lastEvidenceAt'])
    # Preserve the existing recent-scene budget; older open projects fill spare slots.
    nearby = sorted(nearby, key=key, reverse=True)[:16]
    return nearby + sorted(ongoing, key=key, reverse=True)[:16-len(nearby)]


def coverage(refs, sources, *, gap_seconds=30):
    stamps = sorted({datetime.fromisoformat(sources[r]['at']) for r in refs})
    gaps = [{'from': a.isoformat(), 'to': b.isoformat(), 'seconds': (b-a).total_seconds()}
            for a, b in zip(stamps, stamps[1:]) if (b-a).total_seconds() > gap_seconds]
    return {'firstEvidenceAt': stamps[0].isoformat(), 'lastEvidenceAt': stamps[-1].isoformat(),
            'evidenceSpanSeconds': (stamps[-1]-stamps[0]).total_seconds(), 'continuousDurationSeconds': None,
            'sampleCount': sum(sources[r]['kind'] == 'sampled-observation' for r in refs),
            'statementCount': sum(sources[r]['kind'] == 'user-statement' for r in refs),
            'gaps': gaps, 'meaning': '证据覆盖跨度，不是连续活动时长；未采样时段未知。'}


def validate_updates(value, state, batch):
    if not isinstance(value, dict) or set(value) != {'updates'} or not isinstance(value['updates'], list) or len(value['updates']) > 16:
        raise ValueError('invalid event updates')
    allowed = {s['sourceId'] for s in batch}
    result, changed = [], set()
    for update in value['updates']:
        if not isinstance(update, dict) or set(update) != {'eventId', 'title', 'summary', 'phase', 'status', 'sourceIds', 'uncertainties'}:
            raise ValueError('invalid event fields')
        eid = update['eventId']
        if eid is not None and (not isinstance(eid, str) or eid not in state['events'] or eid in changed):
            raise ValueError('unknown or duplicate event')
        for key, limit in (('title', 100), ('summary', 1000), ('phase', 200)):
            if not isinstance(update[key], str) or not 0 < len(update[key].strip()) <= limit:
                raise ValueError('invalid event text')
        previous = state['events'].get(eid, {})
        refs = update['sourceIds']
        known = allowed | set(previous.get('sourceIds', []))
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in known for r in refs):
            raise ValueError('event requires current source citations')
        if not any(r in allowed for r in refs):
            # An unchanged old event is not a new stage. Retain the raw model
            # response for audit but do not repeat it or apply unsupported edits.
            continue
        if update['status'] not in {'ongoing', 'paused', 'completed', 'uncertain'}:
            raise ValueError('invalid event status')
        if not isinstance(update['uncertainties'], list) or len(update['uncertainties']) > 12 or any(not isinstance(s, str) or len(s) > 500 for s in update['uncertainties']):
            raise ValueError('invalid event uncertainties')
        if previous.get('status') == 'completed' and update['status'] != 'completed':
            raise ValueError('completed events require a new occurrence')
        refs = list(dict.fromkeys(refs))
        all_refs = list(dict.fromkeys(previous.get('sourceIds', []) + refs))
        cov = coverage(all_refs, state['sources'])
        phase = {'description': update['phase'], 'title': update['title'], 'status': update['status'], 'sourceIds': refs,
                 'at': max((state['sources'][r]['at'] for r in refs), key=datetime.fromisoformat)}
        result.append({**update, 'eventId': eid or 'event-' + uuid4().hex[:16],
                       'sourceIds': all_refs, 'phases': previous.get('phases', []) + [phase],
                       'coverage': cov, 'schemaVersion': 'long-event.v1',
                       'revision': previous.get('revision', 0) + 1,
                       'updatedAt': datetime.now(timezone.utc).isoformat(),
                       'claim': 'derived-hypothesis-not-independent-evidence'})
        changed.add(eid)
    return result


def project_to_timeline(timeline, events, *, user_id, case_id):
    """Write derived long events as retrievable long-tier ``event`` cards.

    Long events used to live only in the journal (``long_event.batch``), so
    ``TimelineMemory.retrieve`` could never see them. Each revision becomes one
    card that replaces the previous revision's card. The card keeps the source
    ids, so ``forget_sources`` still cascades, and it is labelled as a derived
    hypothesis, never as independent evidence.
    """
    written = []
    for event in events:
        revision = int(event.get('revision', 1))
        card_id = f"longevent-{event['eventId']}-r{revision}"
        # Replace whichever earlier revision is currently active, not just r-1:
        # if writing r-1 had failed, r-2 would otherwise stay active alongside.
        prefix = f"longevent-{event['eventId']}-r"
        active = [card for card in timeline.cards(
                      user_id=user_id, case_id=case_id, scope='case', kinds=('event',))
                  if card['id'].startswith(prefix) and card['id'] != card_id
                  and int(card['id'][len(prefix):] or 0) < revision]
        replaces = max(active, key=lambda c: int(c['id'][len(prefix):]))['id'] if active else None
        coverage_value = event.get('coverage') or {}
        first, last = coverage_value.get('firstEvidenceAt'), coverage_value.get('lastEvidenceAt')
        phases = '；'.join(p.get('description', '') for p in event.get('phases', [])[-6:] if p.get('description'))
        text = '\n'.join(part for part in (event.get('title'), event.get('summary'),
                                           ('阶段：' + phases) if phases else None) if part)
        span = first and last and datetime.fromisoformat(last) > datetime.fromisoformat(first)
        card = timeline.remember(
            text, user_id=user_id, case_id=case_id, scope='case', kind='event', tier='long',
            happened_at=first, recorded_at=event.get('updatedAt') or last, effective_at=first,
            effective_end=last if span else None,
            source_ids=list(event.get('sourceIds', [])), importance=0.8, supersede=False,
            card_id=card_id, replaces_card_id=replaces,
            metadata={'producer': 'long-events', 'timeBasis': 'measured', 'longEventId': event['eventId'],
                      'revision': revision, 'eventStatus': event.get('status'),
                      'uncertainties': list(event.get('uncertainties', [])),
                      'claim': 'derived-hypothesis-not-independent-evidence'})
        if card:
            written.append(card)
    return written


class LongEvents:
    def __init__(self, journal, key, *, on_batch=None):
        self.journal = journal
        self.service = AnswerService(key, model=os.getenv('EGOASSIST_EVENT_MODEL', 'qwen-flash'), timeout=30)
        self.task = None
        self.lock = asyncio.Lock()
        self.last_attempt = 0
        self.closed = False
        # Optional consumer of each validated batch (e.g. timeline projection).
        # Failures there never fail derivation: the journal stays the source of truth.
        self.on_batch = on_batch

    def _publish(self, updates):
        if not self.on_batch or not updates:
            return
        try:
            self.on_batch(updates)
        except Exception as exc:
            self.journal.append('long_event.projection_failed', {'error': type(exc).__name__ + ': ' + str(exc)[:180],
                                                                  'eventIds': [u['eventId'] for u in updates]})

    def schedule(self, *, force=False):
        if self.closed or not self.service.key or self.task and not self.task.done():
            return
        if not force and time.monotonic() - self.last_attempt < 20:
            return
        state = projection(self.journal)
        if not force and state['failures'] >= 3:
            return
        if not pending(state):
            return
        self.task = asyncio.create_task(self._worker(), name='long-event-derivation')
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def _worker(self):
        while not self.closed:
            await self.run()
            state = projection(self.journal)
            if (state['job'] or {}).get('status') == 'failed' or not pending(state):
                return
            await asyncio.sleep(20)

    async def run(self, *, max_batches=3):
        async with self.lock:
            for _ in range(max_batches):
                state = projection(self.journal)
                batch = next_batch(state)
                if not batch:
                    return
                self.last_attempt = time.monotonic()
                recent = candidate_events(state, batch)
                model_state = {**state, 'events': {e['eventId']: e for e in recent}}
                messages = [{'role': 'system', 'content': prompt_text('long_events/derive')},
                            {'role': 'user', 'content': json.dumps({'historicalContextOnly': True, 'events': [
                                {**{k: e[k] for k in ('eventId', 'title', 'summary', 'status', 'coverage', 'uncertainties')},
                                 'phases': e['phases'][-6:],
                                 'sources': [{**state['sources'][ref], 'text': state['sources'][ref]['text'][:1200],
                                              'textTruncated': len(state['sources'][ref]['text']) > 1200}
                                             for ref in list(dict.fromkeys(e['sourceIds'][:1] + e['sourceIds'][-3:]))],
                                 'sourcesTruncated': len(e['sourceIds']) > 4} for e in recent]}, ensure_ascii=False)},
                            {'role': 'user', 'content': json.dumps({
                                'task': '仅根据本条消息的新证据判断本批事件变化；历史摘要不能覆盖新证据。',
                                'sources': [{**s, 'text': s['text'][:2000], 'textTruncated': len(s['text']) > 2000} for s in batch]}, ensure_ascii=False)}]
                deps = tuple(dict.fromkeys([s['sourceId'] for s in batch] + [ref for e in recent for ref in e['sourceIds']])) + ((state['revision'],) if state['revision'] else ())
                request = self.journal.append('long_event.request', {'model': self.service.model, 'messages': messages,
                    'sourceIds': list(deps), 'baseRevision': state['revision']}, required_event_ids=deps)
                self.journal.append('long_event.job', {'status': 'running', 'requestId': request['id']})
                started = time.perf_counter()
                try:
                    from egoassist.core.model_audit import record_request, record_output
                    parameters = {
                        'temperature': 0, 'enable_thinking': False, 'max_tokens': 3000,
                        'result_format': 'message', 'response_format': {'type': 'json_object'}}
                    record_request(self.journal, request, {'model': self.service.model, 'input': {'messages': messages}, 'parameters': parameters})
                    payload = await self.service.complete(messages, model=self.service.model, parameters=parameters)
                    record_output(self.journal, request, payload)
                    choice = payload['output']['choices'][0]
                    raw = choice['message']['content']
                    self.journal.append('long_event.response', {'requestId': request['id'], 'providerRequestId': payload.get('request_id'),
                        'rawOutput': raw, 'usage': payload.get('usage'), 'latencyMs': round((time.perf_counter()-started)*1000, 1)}, required_event_ids=deps)
                    if choice.get('finish_reason') not in {None, 'stop'}:
                        raise ValueError('incomplete event response')
                    parsed = json.loads(raw)
                    updates = validate_updates(parsed, model_state, batch)
                    if projection(self.journal)['revision'] != state['revision']:
                        raise ValueError('event state changed during generation')
                    self.journal.append('long_event.batch', {'events': updates, 'processedSourceIds': [s['sourceId'] for s in batch],
                        'requestId': request['id'], 'model': self.service.model, 'providerRequestId': payload.get('request_id'),
                        'ignoredWithoutNewEvidence': len(parsed['updates']) - len(updates)}, required_event_ids=deps)
                    self.journal.append('long_event.job', {'status': 'completed', 'requestId': request['id']})
                    self._publish(updates)
                except asyncio.CancelledError:
                    self.journal.append('long_event.job', {'status': 'queued', 'requestId': request['id']})
                    raise
                except Exception as exc:
                    self.journal.append('long_event.job', {'status': 'failed', 'requestId': request['id'], 'error': type(exc).__name__ + ': ' + str(exc)[:180]})
                    return

    def snapshot(self, *, before=None, limit=12, source_limit=12):
        state = projection(self.journal)
        boundary = datetime.fromisoformat(before) if before else None
        events = []
        for value in state['events'].values():
            if any(r not in state['sources'] for r in value['sourceIds']):
                continue
            if boundary and (datetime.fromisoformat(value['updatedAt']) > boundary or
                             datetime.fromisoformat(value['coverage']['lastEvidenceAt']) > boundary):
                continue
            events.append(value)
        events.sort(key=lambda e: datetime.fromisoformat(e['coverage']['lastEvidenceAt']), reverse=True)
        selected = deepcopy(events[:limit])
        for event in selected:
            refs = event['sourceIds']
            event['totalSources'] = len(refs)
            event['sourceIds'] = refs[-source_limit:]
            event['sourcesTruncated'] = len(refs) > source_limit
            event['sources'] = [{**state['sources'][ref], 'text': state['sources'][ref]['text'][:1200],
                                 'textTruncated': len(state['sources'][ref]['text']) > 1200} for ref in event['sourceIds']]
            event['totalPhases'] = len(event['phases'])
            event['phases'] = event['phases'][-12:]
            event['coverage']['gapCount'] = len(event['coverage']['gaps'])
            event['coverage']['gaps'] = event['coverage']['gaps'][-12:]
            # Preserve a truthful idle/gap indicator without inventing completion.
            reference = boundary or datetime.now(timezone.utc)
            event['observationStale'] = (reference - datetime.fromisoformat(event['coverage']['lastEvidenceAt'])).total_seconds() > 30
        return {'events': selected, 'totalEvents': len(events), 'truncated': len(events) > limit,
                'pendingSources': len(pending(state)), 'job': state['job'],
                'automaticRetryPaused': state['failures'] >= 3,
                'meaning': '按采样证据推断的事件；中断不等于结束，跨度不等于持续时长。'}

    def evidence(self, event_id, *, offset=0, limit=24, source_ids=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid evidence page')
        state = projection(self.journal)
        event = state['events'].get(event_id)
        if not event or any(ref not in state['sources'] for ref in event['sourceIds']):
            raise ValueError('event evidence unavailable')
        refs = event['sourceIds']
        if source_ids is not None:
            if (not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 100
                    or any(not isinstance(ref, str) or ref not in refs for ref in source_ids)):
                raise ValueError('source does not belong to this event')
            refs = list(dict.fromkeys(source_ids))
        selected = refs[offset:offset+limit]
        return {'eventId': event_id, 'sources': [state['sources'][ref] for ref in selected],
                'total': len(refs), 'nextOffset': offset+len(selected) if offset+len(selected) < len(refs) else None}

    async def rebuild(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.journal.append('long_event.reset', {'reason': 'explicit-rebuild', 'schemaVersion': 'long-event.v1'})
        self.schedule(force=True)

    async def close(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.service.close()
