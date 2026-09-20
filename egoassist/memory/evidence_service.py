"""Source ingestion and evidence snapshots, independent of interaction controls."""
from pathlib import Path
import re

from egoassist.core.journal import SessionJournal
from egoassist.core.source_events import history_event
from .source_index import SourceIndex


class EvidenceService:
    def __init__(self, users_root):
        self.root = Path(users_root)
        self.paused = set()

    def index(self, user):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', user) or user in self.paused:
            raise ValueError('source account unavailable')
        return SourceIndex(self.root / user)

    def ingest(self, user, journal, events):
        if not isinstance(events, list) or not 1 <= len(events) <= 500:
            raise ValueError('import requires 1 to 500 independent events')
        values = [history_event(event) for event in events]
        if len({v['id'] for v in values}) != len(values):
            raise ValueError('duplicate source IDs in batch')
        previous = {row['source']['id']: row for row in journal.entries(types={'source.event'})}
        deleted = {row['sourceEventId'] for row in journal.entries(types={'source.deleted'})}
        erased_keys = {row.get('idempotencyKey') for row in journal.entries(types={'retention.deleted'})}
        for value in values:
            if 'history:' + value['id'] in erased_keys:
                raise ValueError('source was erased and cannot be reimported under the same ID')
            old = previous.get(value['id'])
            if old and (old['source'] != value or old['id'] in deleted):
                raise ValueError('source IDs are immutable; corrections require a new ID and supersedes')
            if value.get('supersedes') and value['supersedes'] not in previous:
                raise ValueError('correction target is not in this conversation')
        accepted = []
        for value in values:
            row = journal.append('source.event', {'source': value}, idempotency_key='history:' + value['id'])
            if row.get('type') != 'source.event':
                raise ValueError('source was erased and cannot be reimported under the same ID')
            accepted.append({'id': value['id'], 'sourceId': journal.session_id + ':' + row['id'],
                'accepted': True, 'indexed': False, 'extraction': 'not-requested', 'vector': 'not-indexed'})
        try:
            self.index(user).sync()
        except Exception as exc:
            return {'events': accepted, 'status': 'accepted-index-pending', 'indexError': type(exc).__name__}
        for row in accepted:
            row['indexed'] = True
        return {'events': accepted, 'status': 'indexed'}

    def query(self, user, session, question, *, exclude=(), snapshot_id=None):
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 10000:
            raise ValueError('question must contain 1 to 10000 characters')
        index = self.index(user)
        return index.snapshot(snapshot_id, session, question) if snapshot_id else index.search(question, session, exclude=exclude)

    async def pause_user(self, user):
        self.paused.add(user)

    def invalidate_user(self, user):
        # All originals remain in journals; this cache and its snapshots are disposable.
        path = self.root / user / 'source-index.sqlite3'
        path.unlink(missing_ok=True)

    def resume_user(self, user):
        self.paused.discard(user)
