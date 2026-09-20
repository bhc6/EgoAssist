"""Small optional composition adapter for the independent diary module."""
import asyncio
from datetime import date
import json
from zoneinfo import ZoneInfo

from aiohttp import web
from .generator import SECTIONS
from .service import DiaryService
from .sources import period_for


def mount(app, *, users_root, timeline_path, api_key, user_key, blocked):
    service=DiaryService(users_root,timeline_path,api_key,blocked)

    def user(request):
        uid=request[user_key]['id']
        (service.root/uid).mkdir(parents=True,exist_ok=True)
        return uid

    async def listing(request):
        store=service.store(user(request))
        kind=request.query.get('kind') or None
        period=period_for(kind,request.query['anchor'],request.query.get('timezone','Asia/Shanghai')) if request.query.get('anchor') else None
        result=store.list(before=request.query.get('before'),kind=kind,period=period)
        result['schedule']=store.setting('schedule') or {'enabled':False,'timezone':'Asia/Shanghai','kinds':['daily','weekly','monthly']}
        return web.json_response(result)

    async def submit(request):
        payload=await request.json()
        timezone=payload.get('timezone','Asia/Shanghai');ZoneInfo(timezone)
        period=period_for(payload.get('kind','daily'),payload.get('anchor'),timezone)
        return web.json_response(service.submit(user(request),period),status=202)

    async def detail(request):
        row=await service.get(user(request),request.match_info['id'])
        # Full evidence is on demand; internal intermediate checkpoints stay out of the reading response.
        public={k:v for k,v in row.items() if k not in {'nodes'}}
        return web.json_response(public)

    async def cancel(request):
        await service.cancel(user(request),request.match_info['id'])
        return web.json_response({'cancelled':True})

    async def retry(request):
        uid=user(request); row=service.store(uid).get(request.match_info['id'])
        if row['status'] not in {'failed','cancelled'}: raise ValueError('仅能重试未完成的整理')
        if uid in service.paused or service.blocked(uid): raise ValueError('账户维护中')
        if len(service.tasks)>=16: raise ValueError('日记队列已满')
        row.update(status='queued',phase='等待重试');row.pop('error',None)
        service.store(uid).put(row);service.schedule(uid,row)
        return web.json_response(row,status=202)

    async def note(request):
        payload=await request.json(); day=date.fromisoformat(payload['day']).isoformat()
        text=payload.get('text')
        if not isinstance(text,str) or not 1<=len(text.strip())<=10000: raise ValueError('补记需要 1 至 10000 个字符')
        return web.json_response(service.store(user(request)).note(day,text.strip()),status=201)

    async def feedback(request):
        row=await service.get(user(request),request.match_info['id']); payload=await request.json()
        index,state=payload.get('item'),payload.get('state')
        if (type(index) is not int or not 0<=index<len(row.get('body',{}).get('suggestions',[]))
            or state not in {'planned','dismissed','done'}): raise ValueError('建议或反馈状态无效')
        return web.json_response(service.store(user(request)).feedback(row['id'],index,state))

    async def settings(request):
        payload=await request.json()
        enabled=payload.get('enabled'); timezone=payload.get('timezone','Asia/Shanghai')
        kinds=payload.get('kinds',['daily','weekly','monthly']);ZoneInfo(timezone)
        if type(enabled) is not bool or not isinstance(kinds,list) or not kinds or not set(kinds)<={'daily','weekly','monthly'}:
            raise ValueError('日记自动归档设置无效')
        return web.json_response(service.store(user(request)).setting('schedule',
            {'enabled':enabled,'timezone':timezone,'kinds':sorted(set(kinds))}))

    async def export(request):
        row=await service.get(user(request),request.match_info['id'])
        if row['status'] not in {'completed','empty'}: raise ValueError('日记尚未完成或来源已变化')
        body=row['body']; labels={'overview':'这段时间','life':'生活','work':'工作','changes':'进展与变化','pending':'未完成','suggestions':'下一步建议'}
        lines=['# '+body['title'],row['period']['start']+' — '+row['period']['end']+'（结束时刻不含）','']
        for key in SECTIONS:
            lines.extend(['## '+labels[key],''])
            for item in body[key]: lines.append('- '+item['text']+' [来源：'+', '.join(item['sourceIds'])+']')
            lines.append('')
        lines.extend(['## 我的补记','',*[n['day']+' '+n['text'] for n in row['notes']],
                      '', '## 来源及覆盖', '', '```json',json.dumps({'coverage':row['coverage'],'sources':row['sources'],'feedback':row['feedback']},ensure_ascii=False,indent=2),'```'])
        return web.Response(text='\n'.join(lines),content_type='text/markdown',
            headers={'Content-Disposition':'attachment; filename="diary-'+row['id']+'.md"'})

    @web.middleware
    async def lifecycle(request,handler):
        uid=(request.get(user_key) or {}).get('id')
        maintenance=uid and request.path in {'/api/privacy/delete','/api/memory/delete'} and request.method=='POST'
        if maintenance: await service.pause_user(uid)
        try:
            response=await handler(request)
            if maintenance and response.status<300:
                service.invalidate_user(uid)
            if (uid and request.path=='/api/privacy/export' and request.method=='GET'
                    and response.status==200 and (service.root/uid/'diary.sqlite3').exists()):
                store=service.store(uid)
                with store.db() as db:
                    identifiers=[r['id'] for r in db.execute('SELECT id FROM entries ORDER BY created')]
                    notes=[dict(r) for r in db.execute('SELECT * FROM notes ORDER BY day,created')]
                    feedback_rows=[dict(r) for r in db.execute('SELECT * FROM feedback')]
                diaries=[]
                for identifier in identifiers:
                    row=await service.get(uid,identifier)
                    diaries.append({k:v for k,v in row.items() if k!='nodes'})
                content=json.loads(response.text)
                content['personalDiary']={'entries':diaries,'notes':notes,'feedback':feedback_rows,
                    'schedule':store.setting('schedule')}
                response.text=json.dumps(content,ensure_ascii=False)
            return response
        finally:
            if maintenance: service.resume_user(uid)

    async def context(_app):
        worker=asyncio.create_task(service.worker(),name='diary-scheduler')
        yield
        worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        await service.close()

    app.middlewares.append(lifecycle)
    app.cleanup_ctx.append(context)
    app.add_routes([web.get('/api/diary',listing),web.post('/api/diary',submit),
        web.post('/api/diary/notes',note),web.post('/api/diary/settings',settings),
        web.get('/api/diary/{id}',detail),web.post('/api/diary/{id}/cancel',cancel),
        web.post('/api/diary/{id}/retry',retry),web.post('/api/diary/{id}/feedback',feedback),
        web.get('/api/diary/{id}/export',export)])
    return service
