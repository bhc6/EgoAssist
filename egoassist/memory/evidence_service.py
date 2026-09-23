"""Source ingestion and evidence snapshots, independent of interaction controls."""
import asyncio
import time
from pathlib import Path
from egoassist.core.journal import EvidenceUnavailable
import re

from egoassist.core.journal import SessionJournal
from egoassist.core.source_events import history_event
from .source_index import SourceIndex


class EvidenceService:
    def __init__(self, users_root):
        self.root = Path(users_root)
        self.paused = set()
        self.pause_depth = {}
        self.readers = {}
        self.reader_started = {}
        self.closed = False

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

    async def run_read(self, user, read, *, timeout):
        if self.closed or user in self.paused or len(self.readers) >= 4 or user in self.readers.values():
            raise EvidenceUnavailable('memory read unavailable or at capacity')
        task = asyncio.create_task(asyncio.to_thread(read))
        self.readers[task] = user
        self.reader_started[task] = time.monotonic()
        def done(task):
            self.readers.pop(task, None)
            self.reader_started.pop(task, None)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(done)
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    def status(self, user):
        times = [self.reader_started[task] for task, owner in self.readers.items() if owner == user]
        return {'scope': 'account', 'active': len(times), 'paused': user in self.paused,
                'oldestMs': round((time.monotonic() - min(times)) * 1000, 3) if times else None,
                'perAccountLimit': 1}

    async def pause_user(self, user):
        self.paused.add(user)
        self.pause_depth[user] = self.pause_depth.get(user, 0) + 1
        tasks = [task for task, owner in self.readers.items() if owner == user]
        # Cancelling the awaiter does not stop a thread. Maintenance must wait
        # before erasing files, even after a foreground read timed out.
        if tasks:
            draining = asyncio.gather(*tasks, return_exceptions=True)
            cancelled = False
            while not draining.done():
                try:
                    await asyncio.shield(draining)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    async def close(self):
        self.closed = True
        if self.readers:
            await asyncio.gather(*tuple(self.readers), return_exceptions=True)

    def invalidate_user(self, user):
        # All originals remain in journals; this cache and its snapshots are disposable.
        path = self.root / user / 'source-index.sqlite3'
        path.unlink(missing_ok=True)

    def resume_user(self, user):
        depth = self.pause_depth.get(user, 1) - 1
        if depth:
            self.pause_depth[user] = depth
        else:
            self.pause_depth.pop(user, None)
            self.paused.discard(user)
