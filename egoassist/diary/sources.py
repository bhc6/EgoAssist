"""Read lane evidence without constructing or mutating runtime owners."""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from egoassist.memory.insights import calendar_period
from egoassist.memory.temporal import ClockContext
from egoassist.core.source_events import project_source


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_manifest(data):
    """Semantic evidence identity, excluding rebuildable hints and storage touches."""
    return {record['sourceId']: digest({key: value for key, value in record.items()
            if key not in {'updatedAt', 'derivedEventHints'}}) for record in data['records']}


def snapshot_change(row, current):
    """Append-only activity may extend a snapshot, never invalidate its evidence."""
    previous = row.get('sourceManifest')
    if previous is None:
        return {'invalid': row.get('sourceFingerprint') != current['fingerprint'], 'added': 0}
    manifest = source_manifest(current)
    invalid = not current['coverage']['complete'] or any(manifest.get(ref) != value for ref, value in previous.items())
    # Imported corrections can supersede a source without changing its old row.
    for record in current['records']:
        if record['sourceId'] in previous or record['kind'] != 'historical-dialogue':
            continue
        correction = json.loads(record['text']).get('supersedes')
        external_refs = row.get('sourceExternalRefs', {})
        if correction and (correction in previous or record.get('sessionId', '') + ':' + correction in previous
                           or record.get('sessionId', '') + ':' + correction in external_refs):
            invalid = True
    return {'invalid': invalid, 'added': len(manifest.keys() - previous.keys())}


def period_for(kind, anchor, timezone):
    if kind not in {'daily', 'weekly', 'monthly'}:
        raise ValueError('请选择日记、周回顾或月回顾')
    period = calendar_period(kind, anchor, ClockContext.current(timezone))
    period['anchor'] = period['start'][:10]
    period['basis'] = 'message-recording / observation-capture / memory-event'
    return period


def collect(root: Path, timeline_path: Path, user_id: str, period: dict):
    lower, upper = (datetime.fromisoformat(period[k]) for k in ('start', 'end'))
    records, errors, unplaced = [], [], []
    def add(ref, kind, text, stamp, recorded, **extra):
        at = datetime.fromisoformat(stamp)
        if at.tzinfo is None:
            raise ValueError('source lacks timezone')
        if lower <= at < upper:
            records.append({'sourceId':ref, 'kind':kind, 'text':text,
                'occurredAt':stamp, 'recordedAt':recorded,
                'localDate':at.astimezone(lower.tzinfo).date().isoformat(), **extra})
    for path in sorted((root/'sessions').glob('*.jsonl')):
        jobs, provenance, events = {}, None, {}
        try:
            with path.open(encoding='utf-8') as handle:
                for number, line in enumerate(handle, 1):
                    if not line.strip(): continue
                    try:
                        row = json.loads(line)
                        kind = row.get('type')
                        if kind == 'session': provenance = (row.get('metadata') or {}).get('diaryProvenance')
                        if kind == 'job_ledger': jobs[row['jobId']] = row
                        if kind == 'long_event.reset': events = {}
                        if kind == 'long_event.batch': events.update({e['eventId']:e for e in row['events']})
                        text, stamp = None, row.get('timestamp')
                        if kind == 'source.event':
                            source = project_source(row, path.stem)
                            stamp = source['sourceAt']
                            at = datetime.fromisoformat(stamp) if stamp else None
                            if at is not None and at.tzinfo is None and source.get('timezone'):
                                from zoneinfo import ZoneInfo
                                at = at.replace(tzinfo=ZoneInfo(source['timezone']))
                            if at is None or at.tzinfo is None:
                                unplaced.append({'sourceId':source['sourceId'], 'sourceAt':stamp,
                                    'sourceHash':source['sourceHash'], 'reason':'unknown-source-time-or-timezone'})
                            else:
                                add(source['sourceId'], 'historical-dialogue',
                                    json.dumps({'speaker':source['speaker'],'text':source['text'],
                                        'supersedes':source['supersedes']}, ensure_ascii=False),
                                    at.isoformat(), source['receivedAt'], sessionId=path.stem,
                                    externalId=source['externalId'])
                        elif kind == 'message' and row.get('status') == 'completed' and row.get('message', {}).get('role') == 'user':
                            text = row['message']['content']; kind = 'user-statement'
                        elif kind == 'message' and row.get('status') == 'completed' and row.get('message', {}).get('role') == 'assistant':
                            text = row['message']['content']; kind = 'assistant-delivery'
                        elif kind == 'ambient_observation' and row.get('status') == 'accepted':
                            evidence = row.get('evidence') or {}
                            if evidence.get('visibleEvidence'):
                                stamp = evidence.get('capturedAt') or stamp
                                text = json.dumps({k:evidence.get(k) for k in ('visibleEvidence','uncertainties','media')}, ensure_ascii=False)
                                kind = 'sampled-observation'
                        elif kind == 'tool_result':
                            text = json.dumps({k:row.get(k) for k in ('name','result','isError')}, ensure_ascii=False)
                            kind = 'execution-receipt'
                        elif kind == 'job_state' and row.get('status') in {'completed','failed','cancelled'}:
                            job = jobs.get(row.get('jobId'), {})
                            detail=row.get('detail') or {};payload=job.get('payload') or {}
                            text = json.dumps({'status':row['status'],
                                'detail':{k:v for k,v in detail.items() if k in {'response','result','error','reason','outcome','text'}},
                                'jobKind':job.get('kind'),
                                'request':{k:v for k,v in payload.items() if k in {'text','question','label','dueAt'}},
                                'jobId':row.get('jobId')}, ensure_ascii=False)
                            kind = 'job-outcome'
                        if text is not None:
                            add(path.stem+':'+row['id'], kind, text, stamp, row['timestamp'], sessionId=path.stem,
                                provenance=provenance)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        errors.append({'file':path.name,'line':number})
        except (OSError, UnicodeError): errors.append({'file':path.name,'error':'unreadable'})
        # Event summaries are navigation aids attached to original evidence,
        # never extra independent sources or replacements for raw observations.
        by_source = {r['sourceId']:r for r in records if r.get('sessionId') == path.stem}
        for event in events.values():
            for phase in event.get('phases', []):
                for ref in phase.get('sourceIds', []):
                    record = by_source.get(path.stem+':'+ref)
                    if record is not None:
                        record.setdefault('derivedEventHints', []).append({
                            'eventId':event['eventId'], 'title':event['title'], 'phase':phase['description'],
                            'statusAtSource':phase['status'], 'claim':'rebuildable-hypothesis; original source takes precedence'})
    if timeline_path.exists():
        # SQLite read-only URI prevents schema creation, migration and writes.
        with sqlite3.connect(timeline_path.resolve().as_uri()+'?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            cards = list(connection.execute("SELECT * FROM memory_cards WHERE user_id=? AND status != 'deleted' ORDER BY happened_at,id", (user_id,)))
        selected = set()
        for card in cards:
            try:
                at = datetime.fromisoformat(card['happened_at'])
                if lower <= at < upper: selected.add(card['id'])
            except (ValueError, TypeError): errors.append({'memoryId':card['id'],'error':'invalid-time'})
        # Later corrections of a period's memory are context, not new events in that period.
        changed = True
        while changed:
            changed = False
            for card in cards:
                if card['supersedes'] in selected and card['id'] not in selected:
                    selected.add(card['id']); changed = True
        for card in cards:
            if card['id'] not in selected: continue
            at = datetime.fromisoformat(card['happened_at'])
            metadata=json.loads(card['metadata'] or '{}')
            records.append({'sourceId':'memory:'+card['id'], 'kind':'memory-state',
                'text':json.dumps({**{k:card[k] for k in ('text','kind','status','supersedes','effective_at','effective_end','source_ids')},
                    'metadata':{k:v for k,v in metadata.items() if k in {'sourceText','grounding','consent','diaryProvenance','uncertainties'}}}, ensure_ascii=False),
                'occurredAt':card['happened_at'],'recordedAt':card['recorded_at'] or card['created_at'],
                'updatedAt':card['updated_at'], 'outsidePeriod':not lower <= at < upper,
                'localDate':at.astimezone(lower.tzinfo).date().isoformat()})
    records.sort(key=lambda x:(datetime.fromisoformat(x['occurredAt']),x['sourceId']))
    counts = Counter(r['localDate'] for r in records if not r.get('outsidePeriod'))
    days, current = [], lower.date()
    today = ClockContext.current(period['timezone']).local_now.date()
    while current < upper.date() and current <= today:
        days.append(current.isoformat()); current += timedelta(days=1)
    coverage = {'records':len(records), 'byDay':dict(sorted(counts.items())),
        'byKind':dict(Counter(r['kind'] for r in records)),
        'noRecordDays':[day for day in days if day not in counts], 'readErrors':errors,
        'unplacedSources':unplaced,
        'omittedRecords':0, 'complete':not errors,
        'meaning':'完整仅表示已读取的记录；无记录不代表没有活动，观察采样不代表连续看见。'}
    return {'records':records,'coverage':coverage,'fingerprint':digest([period,records,errors]+([unplaced] if unplaced else []))}


def chunks(records, budget=14000):
    """No temporal tail truncation. Long individual evidence is split, not dropped."""
    batch, size = [], 0
    for source in records:
        text = source['text']
        for offset in range(0, max(1,len(text)), 3500):
            part = {**source,'text':text[offset:offset+3500], 'part':offset//3500+1}
            cost = len(json.dumps(part, ensure_ascii=False))
            if batch and size+cost > budget:
                yield batch; batch,size = [],0
            batch.append(part); size += cost
    if batch: yield batch
