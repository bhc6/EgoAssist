"""Per-account diary archive. No tables or migrations in existing product databases."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def now(): return datetime.now(timezone.utc).isoformat()


class DiaryStore:
    def __init__(self, root: Path):
        self.path = root/'diary.sqlite3'
        if not root.is_dir(): raise ValueError('账户目录不存在')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                  start TEXT NOT NULL, end TEXT NOT NULL, timezone TEXT NOT NULL, status TEXT NOT NULL,
                  created TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS diary_period ON entries(kind,start,timezone,created);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,day TEXT NOT NULL,text TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feedback(entry_id TEXT NOT NULL,item INTEGER NOT NULL,state TEXT NOT NULL,
                  PRIMARY KEY(entry_id,item));
            ''')

    @contextmanager
    def db(self):
        # Once deleted, a worker must never recreate the account directory.
        if not self.path.parent.is_dir(): raise ValueError('账户目录不存在')
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection: yield connection
        finally: connection.close()

    def create(self, period):
        row = {'id':uuid4().hex,'period':period,'status':'queued','phase':'等待整理',
               'createdAt':now(),'attempts':0}
        with self.db() as db:
            previous = db.execute('SELECT data FROM entries WHERE kind=? AND start=? AND timezone=? AND status IN (\'queued\',\'running\')',
                (period['kind'],period['start'],period['timezone'])).fetchone()
            if previous: return json.loads(previous['data'])
            db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)',
                (row['id'],period['kind'],period['start'],period['end'],period['timezone'],row['status'],row['createdAt'],json.dumps(row,ensure_ascii=False)))
        return row

    def put(self, row):
        with self.db() as db:
            db.execute('UPDATE entries SET status=?,data=? WHERE id=?', (row['status'],json.dumps(row,ensure_ascii=False),row['id']))

    def get(self, identifier):
        with self.db() as db: row = db.execute('SELECT data FROM entries WHERE id=?',(identifier,)).fetchone()
        if not row: raise ValueError('日记不存在')
        return json.loads(row['data'])

    def list(self, *, limit=12, before=None, kind=None, period=None):
        conditions, values = [], []
        if before:
            anchor = self.get(before)
            conditions.append('(created < ? OR (created = ? AND id < ?))')
            values.extend([anchor['createdAt'],anchor['createdAt'],anchor['id']])
        if kind:
            conditions.append('kind=?'); values.append(kind)
        if period:
            conditions.append('start=? AND timezone=?');values.extend([period['start'],period['timezone']])
        where = ' WHERE '+' AND '.join(conditions) if conditions else ''
        with self.db() as db:
            rows = db.execute('SELECT data FROM entries'+where+' ORDER BY created DESC,id DESC LIMIT ?',[*values,limit+1]).fetchall()
        data = [json.loads(row['data']) for row in rows]
        return {'entries':[{k:v for k,v in row.items() if k in {'id','period','status','phase','createdAt','error','coverage'}} for row in data[:limit]],
                'next':data[limit-1]['id'] if len(data)>limit else None}

    def pending(self):
        with self.db() as db:
            return [json.loads(r['data']) for r in db.execute("SELECT data FROM entries WHERE status IN ('queued','running') ORDER BY created")]

    def setting(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else None

    def notes(self, start, end):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM notes WHERE day>=? AND day<? ORDER BY day,created',(start,end))]

    def note(self, day, text):
        row = {'id':uuid4().hex,'day':day,'text':text,'created':now()}
        with self.db() as db: db.execute('INSERT INTO notes VALUES(:id,:day,:text,:created)',row)
        return row

    def feedback(self, identifier, item=None, state=None):
        with self.db() as db:
            if item is not None: db.execute('INSERT OR REPLACE INTO feedback VALUES(?,?,?)',(identifier,item,state))
            return {str(r['item']):r['state'] for r in db.execute('SELECT item,state FROM feedback WHERE entry_id=?',(identifier,))}
