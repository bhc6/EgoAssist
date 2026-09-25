"""Rebuildable FTS projection of immutable journal evidence and scoped snapshots."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time

from egoassist.core.source_events import fingerprint, project_source

STOP = set('a an the is are was were do does did what when where who which how why of to in on at for from and or with has have had it its this that i you he she they my your his her their please tell me about'.split())


def tokens(text):
    words = []
    for word in re.findall(r'[a-zA-Z0-9_]+|[\u3400-\u9fff]+', text.lower()):
        if word in STOP:
            continue
        words.extend([word] if word.isascii() or len(word) < 2 else [word[i:i+2] for i in range(len(word)-1)])
    return list(dict.fromkeys(words))


class SourceIndex:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'source-index.sqlite3'

    @contextmanager
    def db(self):
        if not self.root.is_dir():
            raise ValueError('source account does not exist')
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript('''
                PRAGMA secure_delete=ON;
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, session TEXT, seq INTEGER, hash TEXT, body TEXT);
                CREATE INDEX IF NOT EXISTS source_session ON events(session,seq);
                CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(source UNINDEXED, start UNINDEXED, stop UNINDEXED, terms, tokenize='porter unicode61');
                CREATE TABLE IF NOT EXISTS journals(session TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, inode TEXT, prefix TEXT);
                CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, session TEXT, body TEXT);
            ''')
            with connection:
                yield connection
        finally:
            connection.close()

    def sync(self):
        """Tail append-only files; replacements trigger a transactional session rebuild."""
        with self.db() as db:
            changed_sessions = set()
            paths = {path.stem: path for path in (self.root / 'sessions').glob('*.jsonl')}
            for old in db.execute('SELECT session FROM journals').fetchall():
                if old['session'] not in paths:
                    changed_sessions.add(old['session'])
                    self._clear(db, old['session'])
                    db.execute('DELETE FROM journals WHERE session=?', (old['session'],))
            for session, path in paths.items():
                stat = path.stat()
                saved = db.execute('SELECT * FROM journals WHERE session=?', (session,)).fetchone()
                if saved and saved['size'] == stat.st_size and saved['mtime'] == stat.st_mtime_ns:
                    continue
                with path.open('rb') as handle:
                    prefix = fingerprint(handle.read(min(stat.st_size, 256)).hex())
                    append = saved and stat.st_size > saved['size'] and str(stat.st_ino) == saved['inode'] and prefix == saved['prefix']
                    offset = saved['size'] if append else 0
                    if not append:
                        if saved:
                            changed_sessions.add(session)
                        self._clear(db, session)
                    handle.seek(offset)
                    sequence = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE session=?', (session,)).fetchone()[0]
                    while True:
                        line = handle.readline()
                        if not line or not line.endswith(b'\n'):
                            break
                        offset = handle.tell()
                        row = json.loads(line)
                        if row.get('type') == 'source.deleted':
                            changed_sessions.add(session)
                            sid = session + ':' + row['sourceEventId']
                            db.execute('DELETE FROM passages WHERE source=?', (sid,))
                            db.execute('DELETE FROM events WHERE id=?', (sid,))
                        source = project_source(row, session)
                        if source is None:
                            continue
                        sequence += 1
                        existing = db.execute('SELECT 1 FROM events WHERE id=?', (source['sourceId'],)).fetchone()
                        if existing:
                            db.execute('DELETE FROM passages WHERE source=?', (source['sourceId'],))
                        db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?)',
                            (source['sourceId'], session, sequence, source['sourceHash'], json.dumps(source, ensure_ascii=False)))
                        for start in range(0, len(source['text']), 1640):
                            stop = min(start + 1800, len(source['text']))
                            db.execute('INSERT INTO passages VALUES(?,?,?,?)',
                                (source['sourceId'], start, stop, ' '.join(tokens(source['speaker'] + ' ' + source['text'][start:stop]))))
                    db.execute('INSERT OR REPLACE INTO journals VALUES(?,?,?,?,?)',
                        (session, offset, stat.st_mtime_ns, str(stat.st_ino), prefix))
            for session in changed_sessions:
                self._prune_snapshots(db, session)

    @staticmethod
    def _clear(db, session):
        db.execute('DELETE FROM passages WHERE source IN (SELECT id FROM events WHERE session=?)', (session,))
        db.execute('DELETE FROM events WHERE session=?', (session,))

    @staticmethod
    def _prune_snapshots(db, session):
        for row in db.execute('SELECT id,session,body FROM snapshots WHERE session=?', (session,)).fetchall():
            packet = json.loads(row['body'])
            valid = db.execute('SELECT 1 FROM journals WHERE session=?', (row['session'],)).fetchone()
            for source in packet['sources'] + packet['conversation']:
                current = db.execute('SELECT hash FROM events WHERE id=? AND session=?',
                    (source['sourceId'], row['session'])).fetchone()
                if current is None or current['hash'] != source['sourceHash']:
                    valid = False
                    break
            if not valid:
                db.execute('DELETE FROM snapshots WHERE id=?', (row['id'],))

    def search(self, question, session, *, limit=12, budget=24000, exclude=(), recent_budget=8000, recent_limit=8):
        started = time.perf_counter()
        self.sync()
        terms = tokens(question)[:64]
        with self.db() as db:
            expression = ' OR '.join('"' + term + '"' for term in terms)
            matches, total = [], 0
            if expression:
                params = (expression, session)
                total = db.execute('SELECT COUNT(*) FROM passages JOIN events ON source=id WHERE passages MATCH ? AND session=?', params).fetchone()[0]
                matches = db.execute('SELECT events.*,start,stop,bm25(passages) AS rank FROM passages JOIN events ON source=id WHERE passages MATCH ? AND session=? ORDER BY rank,seq,start LIMIT 256', params).fetchall()
            audit, selected, seen, used = [], [], set(), 0
            def add(row, start, stop, reason):
                nonlocal used
                key = (row['id'], start, stop)
                if row['id'] in exclude or key in seen:
                    return False
                source = json.loads(row['body'])
                excerpt = source['text'][start:stop]
                if used + len(excerpt) > budget:
                    return False
                seen.add(key); used += len(excerpt)
                selected.append({**source, 'text': excerpt, 'span': [start, stop],
                    'fullTextLength': len(source['text']), 'retrievalReason': reason,
                    'supportsSubquestion': question, 'uncertainties': ['Attribution and event time require interpretation; retrieval does not validate claims.']})
                return True
            anchors = []
            for row in matches:
                reason = ('excluded-current-turn' if row['id'] in exclude else
                    'outside-top-k' if len(anchors) >= limit else 'character-budget')
                chosen = len(anchors) < limit and add(row, row['start'], row['stop'], 'full-text')
                if chosen:
                    anchors.append(row); reason = 'selected'
                audit.append({'sourceId': row['id'], 'span': [row['start'], row['stop']], 'score': -row['rank'],
                    'permissionEligible': True, 'semanticFilter': False, 'selected': bool(chosen), 'reason': reason})
            for row in anchors:
                for adjacent in db.execute('SELECT * FROM events WHERE session=? AND seq BETWEEN ? AND ? ORDER BY seq', (session, row['seq']-1, row['seq']+1)):
                    if adjacent['id'] != row['id']:
                        add(adjacent, 0, min(1800, len(json.loads(adjacent['body'])['text'])), 'adjacent-turn')
            recent, remaining = [], recent_budget
            for row in db.execute('SELECT * FROM events WHERE session=? ORDER BY seq DESC LIMIT ?', (session, recent_limit)):
                if row['id'] in exclude:
                    continue
                source = json.loads(row['body']); text = source['text'][:min(1800, remaining)]
                if not text:
                    break
                remaining -= len(text)
                recent.append({**source, 'text': text, 'span': [0, len(text)], 'fullTextLength': len(source['text'])})
            corpus = db.execute('SELECT COUNT(*) FROM events WHERE session=?', (session,)).fetchone()[0]
            packet = {'schema': 'egoassist.evidence-package.v1', 'question': question, 'sessionId': session,
                'sources': selected, 'conversation': list(reversed(recent)), 'conflicts': [],
                'conflictStatus': 'not-adjudicated', 'status': 'retrieved' if selected else 'no-lexical-match',
                'coverage': {'sourceEvents': corpus, 'matchingPassages': total, 'candidatePassages': len(matches),
                    'candidateLimit': 256, 'candidateTruncated': total > len(matches), 'selectedPassages': len(selected),
                    'characters': used, 'characterBudget': budget, 'scope': 'case', 'extractionRequired': False,
                    'anchorLimit': limit, 'recentCharacterBudget': recent_budget, 'recentLimit': recent_limit,
                    'vector': 'not-enabled', 'expansion': 'one-neighbor-per-side'},
                'audit': audit, 'missingInformation': ['Lexical retrieval is not a completeness guarantee; unexamined evidence may exist.'],
                'retrievalMs': round((time.perf_counter()-started)*1000, 3)}
            packet['snapshotId'] = fingerprint(packet)
            db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)', (packet['snapshotId'], session, json.dumps(packet, ensure_ascii=False)))
        return packet

    def snapshot(self, identifier, session, question):
        self.sync()
        with self.db() as db:
            row = db.execute('SELECT body FROM snapshots WHERE id=? AND session=?', (identifier, session)).fetchone()
            if row is None:
                raise ValueError('evidence snapshot unavailable or deleted')
            packet = json.loads(row['body'])
            if packet['question'] != question:
                raise ValueError('evidence snapshot belongs to a different question')
            for source in packet['sources'] + packet['conversation']:
                present = db.execute('SELECT hash FROM events WHERE id=? AND session=?', (source['sourceId'], session)).fetchone()
                if present is None or present['hash'] != source['sourceHash']:
                    raise ValueError('evidence source changed or deleted')
            return packet

    def events(self, session):
        self.sync()
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT body FROM events WHERE session=? ORDER BY seq', (session,))]
