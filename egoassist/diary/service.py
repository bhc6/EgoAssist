"""An independent resumable queue and schedule; never submits runtime actions."""
import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
import sqlite3

from .generator import DiaryGenerator
from .sources import collect, digest, period_for
from .store import DiaryStore, now
from egoassist.memory.temporal import ClockContext

LOGGER = logging.getLogger(__name__)


class DiaryService:
    def __init__(self, users_root, timeline_path, key, blocked=lambda user:False):
        self.root,self.timeline_path = Path(users_root),Path(timeline_path)
        self.generator = DiaryGenerator(key)
        self.blocked = blocked
        self.tasks = {}; self.stopping=False; self.paused=set()
        self.slot = asyncio.Semaphore(1)

    def store(self, uid):
        if not re.fullmatch(r'[A-Za-z0-9_-]+',uid): raise ValueError('账户标识无效')
        return DiaryStore(self.root/uid)

    def sources(self, uid, period):
        data = collect(self.root/uid,self.timeline_path,uid,period)
        diary_path=self.root/uid/'diary.sqlite3'
        with sqlite3.connect(diary_path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            notes=[dict(r) for r in db.execute('SELECT * FROM notes WHERE day>=? AND day<? ORDER BY day,created',
                (period['start'][:10],period['end'][:10]))]
        for note in notes:
            data['records'].append({'sourceId':'note:'+note['id'],'kind':'personal-note',
                'text':note['text'],'occurredAt':note['day'], 'recordedAt':note['created'], 'localDate':note['day']})
        data['coverage']['personalNotes']=len(notes)
        data['coverage']['records']+=len(notes)
        for note in notes:
            counts=data['coverage']['byDay']; counts[note['day']]=counts.get(note['day'],0)+1
        data['coverage']['noRecordDays']=[day for day in data['coverage']['noRecordDays'] if day not in data['coverage']['byDay']]
        data['fingerprint']=digest([data['fingerprint'],notes])
        return data

    def submit(self,uid,period):
        if self.stopping or uid in self.paused or self.blocked(uid): raise ValueError('账户维护中，请稍后重试')
        if len(self.tasks)>=16: raise ValueError('日记整理队列已满，请稍后再试')
        row=self.store(uid).create(period);self.schedule(uid,row)
        return row

    def schedule(self,uid,row):
        key=(uid,row['id'])
        if key in self.tasks or self.stopping or uid in self.paused or self.blocked(uid): return
        task=asyncio.create_task(self.run(uid,row),name='diary-'+row['id'])
        self.tasks[key]=task
        def finished(task):
            self.tasks.pop(key,None)
            if not task.cancelled() and task.exception(): LOGGER.error('Diary worker failed',exc_info=task.exception())
        task.add_done_callback(finished)

    async def run(self,uid,row):
        store=self.store(uid)
        try:
            async with self.slot:
                if self.blocked(uid) or uid in self.paused: return
                row.update(status='running',phase='读取三链路记录',attempts=row.get('attempts',0)+1)
                store.put(row)
                data=await asyncio.to_thread(self.sources,uid,row['period'])
                if not data['coverage']['complete']: raise ValueError('部分来源无法读取，已停止整理，避免漏记')
                if row.get('sourceFingerprint') != data['fingerprint']:
                    row['nodes']={};row['calls']=[]
                row.update(sourceFingerprint=data['fingerprint'],coverage=data['coverage'])
                body,daily=await self.generator.generate(row,data,store.put)
                latest=await asyncio.to_thread(self.sources,uid,row['period'])
                if data['fingerprint']!=latest['fingerprint']:
                    raise ValueError('整理期间来源发生变化，请重新生成；本次未发布旧内容')
                row.update(status='completed' if data['records'] else 'empty',phase='已归档',
                    body=body,daily=daily,sources=data['records'],finishedAt=now())
                store.put(row)
        except asyncio.CancelledError:
            if store.path.exists():
                row.update(status='queued' if self.stopping else 'cancelled',phase='等待恢复' if self.stopping else '已取消')
                store.put(row)
            raise
        except Exception as exc:
            if store.path.exists():
                row.setdefault('failures',[]).append({'at':now(),'phase':row.get('phase'),
                    'error':type(exc).__name__+': '+str(exc)[:200]})
                row.update(status='failed',phase='整理未完成',error=type(exc).__name__+': '+str(exc)[:200])
                store.put(row)

    async def get(self,uid,identifier):
        store=self.store(uid); row=store.get(identifier)
        if row.get('sourceFingerprint'):
            current=await asyncio.to_thread(self.sources,uid,row['period'])
            if current['fingerprint']!=row['sourceFingerprint']:
                if row['status'] in {'queued','running'}:
                    await self.cancel(uid,identifier)
                # Don't expose deleted/corrected source text through a historical snapshot.
                row={k:v for k,v in row.items() if k not in {'body','daily','sources','nodes','calls'}}
                row.update(status='stale',phase='来源已变化，请重新整理')
                store.put(row)
        row['feedback']=store.feedback(identifier)
        row['notes']=store.notes(row['period']['start'][:10],row['period']['end'][:10])
        return row

    async def cancel(self,uid,identifier):
        row=self.store(uid).get(identifier)
        task=self.tasks.get((uid,identifier))
        if task:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        elif row['status'] in {'queued','running'}:
            row.update(status='cancelled',phase='已取消');self.store(uid).put(row)

    async def pause_user(self,uid):
        self.paused.add(uid)
        tasks=[task for (user,_),task in list(self.tasks.items()) if user==uid]
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    def invalidate_user(self,uid):
        if not (self.root/uid/'diary.sqlite3').exists(): return
        store=self.store(uid)
        with store.db() as db:
            import json
            rows=[json.loads(r['data']) for r in db.execute('SELECT data FROM entries')]
        for row in rows:
            row={k:v for k,v in row.items() if k not in {'body','sources','daily','nodes','calls'}}
            row.update(status='stale',phase='来源维护后需重新整理');store.put(row)

    def resume_user(self,uid):
        self.paused.discard(uid)

    async def tick(self):
        for path in self.root.glob('*/diary.sqlite3'):
            uid=path.parent.name
            if self.blocked(uid) or uid in self.paused: continue
            store=self.store(uid)
            for row in store.pending():
                if len(self.tasks)<16: self.schedule(uid,row)
            settings=store.setting('schedule') or {}
            if not settings.get('enabled'): continue
            clock=ClockContext.current(settings['timezone']);today=clock.local_now.date()
            # Generate the most recently closed natural periods. Missed historical periods remain manually selectable.
            anchors={'daily':today-timedelta(days=1),
                     'weekly':today-timedelta(days=today.weekday()+1),
                     'monthly':today.replace(day=1)-timedelta(days=1)}
            for kind in settings['kinds']:
                period=period_for(kind,anchors[kind].isoformat(),settings['timezone'])
                key='scheduled:'+kind+':'+period['start']
                if not store.setting(key) and len(self.tasks)<16:
                    row=self.submit(uid,period);store.setting(key,row['id'])

    async def worker(self):
        while not self.stopping:
            try: await self.tick()
            except Exception: LOGGER.exception('Diary scheduling cycle failed')
            await asyncio.sleep(30)

    async def close(self):
        self.stopping=True
        tasks=list(self.tasks.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
