"""Bounded hierarchical compression. Every source chunk is processed or the job fails."""
from collections import defaultdict
import json
import os
import re
import time

import aiohttp

from egoassist.prompts import prompt_text
from .sources import chunks, digest

SECTIONS = ('overview','life','work','changes','pending','suggestions')


class InvalidDiaryResponse(ValueError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt=receipt


class DiaryProviderError(RuntimeError):
    """Safe provider diagnostics, without echoing source text or credentials."""
    def __init__(self, status, payload, receipt):
        payload = payload if isinstance(payload, dict) else {}
        def identifier(value):
            return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) else None
        code = identifier(payload.get('code'))
        receipt.update(status='provider-error', httpStatus=status, providerCode=code,
                       providerRequestId=identifier(payload.get('request_id')))
        if code == 'DataInspectionFailed':
            message = '日记整理被模型服务的内容审核拒绝，未生成完整日记。已完成的分块已保留；请联系模型服务支持核查，直接重试可能仍会失败。'
        elif status == 401:
            message = '日记模型密钥无效或已失效，请检查服务配置。'
        elif status == 429:
            message = '日记模型请求受限，请稍后重试并检查服务额度。'
        elif status >= 500:
            message = '日记模型服务暂时异常，请稍后重试。'
        else:
            message = '日记模型拒绝了请求，请根据错误码和请求编号检查服务配置。'
        super().__init__(message + f'（HTTP {status}' + (f'，{code}' if code else '') + '）')
        self.receipt = receipt


def validate(value, allowed):
    if not isinstance(value, dict) or set(value) != {'title',*SECTIONS}:
        raise ValueError('日记输出结构不完整')
    if not isinstance(value['title'],str) or not 1 <= len(value['title'].strip()) <= 80:
        raise ValueError('日记标题无效')
    for key in SECTIONS:
        maximum = 2 if key == 'overview' else 3 if key == 'suggestions' else 6
        if not isinstance(value[key],list) or len(value[key])>maximum: raise ValueError('日记栏目无效')
        for claim in value[key]:
            if (not isinstance(claim,dict) or set(claim) != {'text','sourceIds'}
                or not isinstance(claim['text'],str) or not 1 <= len(claim['text'].strip()) <= 350
                or not isinstance(claim['sourceIds'],list) or not 1 <= len(claim['sourceIds']) <= 12
                or any(not isinstance(ref,str) or ref not in allowed for ref in claim['sourceIds'])):
                raise ValueError('日记条目缺少有效原始引用')
    return value


def references(body):
    return {ref for key in SECTIONS for item in body[key] for ref in item['sourceIds']}


class DiaryGenerator:
    def __init__(self, key):
        self.key = key
        self.model = os.getenv('EGOASSIST_DIARY_MODEL','qwen3-max')
        self.policy = prompt_text('diary/generate')+'\nsourceIds 只允许使用本请求 providedSourceIds 中的短编号 S1 等。不要引用证据正文内嵌的其他 ID。所有引用只放 sourceIds 字段，title/text 正文中禁止出现 S 编号、来源编号或引用括号。assistant-delivery 仅证明助手交付过这一段文字或草稿，可以据此确认写稿动作完成，但其中内容并非用户事实或现实任务已完成的证据。不要把已交付的草稿误说为仍未生成；也不要把模型回答当作现实验证结果。'

    async def call(self, stage, period, inputs, allowed):
        if not self.key: raise ValueError('未配置日记模型密钥')
        import dashscope
        aliases={ref:'S'+str(index+1) for index,ref in enumerate(sorted(allowed))}
        def project(value):
            if isinstance(value,list): return [project(v) for v in value]
            if isinstance(value,dict):
                return {k:aliases.get(v,v) if k=='sourceId' and isinstance(v,str)
                        else [aliases.get(ref,ref) for ref in v] if k=='sourceIds' and isinstance(v,list)
                        else project(v) for k,v in value.items()}
            return value
        messages = [{'role':'system','content':self.policy}, {'role':'user','content':json.dumps(
            {'stage':stage,'period':period,'providedSourceIds':list(aliases.values()),'inputs':project(inputs)},ensure_ascii=False)}]
        started = time.perf_counter()
        receipt={'model':self.model,'stage':stage,'inputHash':digest(messages),
                 'sourceIds':sorted(allowed),'sourceMap':{v:k for k,v in aliases.items()}}
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=90)) as client:
            async with client.post(dashscope.base_http_api_url.rstrip('/')+'/services/aigc/text-generation/generation',
                headers={'Authorization':'Bearer '+self.key},json={'model':self.model,'input':{'messages':messages},
                'parameters':{'result_format':'message','temperature':0,'enable_thinking':False,
                              'max_tokens':4000,'response_format':{'type':'json_object'}}}) as response:
                if response.status != 200:
                    try:
                        error = await response.json(content_type=None)
                    except (ValueError, UnicodeError):
                        error = {}
                    receipt['latencyMs'] = round((time.perf_counter()-started)*1000)
                    raise DiaryProviderError(response.status, error, receipt)
                payload = await response.json()
        choice = payload['output']['choices'][0]
        raw = choice['message']['content']
        receipt.update({'providerRequestId':payload.get('request_id'),
            'usage':payload.get('usage'), 'latencyMs':round((time.perf_counter()-started)*1000),
            'rawOutput':raw})
        try:
            if choice.get('finish_reason') not in {None,'stop'}: raise ValueError('模型输出被截断，未保存为完成日记')
            body=validate(json.loads(raw),set(aliases.values()))
            for key in SECTIONS:
                for item in body[key]: item['sourceIds']=[receipt['sourceMap'][ref] for ref in item['sourceIds']]
        except (ValueError,TypeError,KeyError) as exc:
            receipt['status']='invalid-output'
            raise InvalidDiaryResponse(str(exc),receipt) from exc
        receipt['status']='validated'
        return body,receipt

    async def generate(self, row, source_data, save):
        records = source_data['records']
        if not records:
            return {'title':'这段时间暂无记录', **{key:[] for key in SECTIONS}}, []
        if sum(len(r['text']) for r in records)>5_000_000:
            raise ValueError('本周期超过单次整理容量，请先按日整理；没有生成残缺报告')
        cached = row.setdefault('nodes',{})
        audit = row.setdefault('calls',[])
        async def infer(stage, inputs, allowed):
            key = digest([self.model,self.policy,row['period'],stage,inputs])
            if key in cached: return cached[key]
            if len(audit)>=500: raise ValueError('本次整理达到请求上限；分块结果已保存')
            row['phase'] = stage; save(row)
            try:
                body, receipt = await self.call(stage,row['period'],inputs,allowed)
            except (InvalidDiaryResponse, DiaryProviderError) as exc:
                audit.append(exc.receipt);save(row)
                raise
            cached[key] = body; audit.append(receipt); save(row)
            return body

        async def reduce(nodes, label):
            level = 0
            while len(nodes)>1:
                batches, batch, size = [],[],0
                for node in nodes:
                    cost = len(json.dumps(node,ensure_ascii=False))
                    if batch and size+cost>18000:
                        batches.append(batch);batch,size=[],0
                    batch.append(node);size+=cost
                if batch: batches.append(batch)
                # Avoid looping if an unusually large model reply cannot fit with its neighbour.
                if len(batches)==len(nodes):
                    raise ValueError('压缩结果过大，无法完整合并，已保留分块')
                nodes = [await infer(label+f' · 汇总第{level+1}层', group,
                            set().union(*(references(node) for node in group))) for group in batches]
                level += 1
                if level>12: raise ValueError('日记压缩层级超过限制')
            return nodes[0]

        days = defaultdict(list)
        for record in records: days[record['localDate']].append(record)
        daily = []
        for day, evidence in sorted(days.items()):
            parts = []
            for index, part in enumerate(chunks(evidence)):
                parts.append(await infer(day+f' · 整理分块 {index+1}',part,{r['sourceId'] for r in part}))
            daily.append({'date':day,'body':await reduce(parts,day),'sourceCount':len(evidence)})
        body = await reduce([{**day['body'],'title':(day['date']+' '+day['body']['title'])[:80]} for day in daily], '周期回顾')
        # Final framing is required even when a period happens to contain just one day.
        body = await infer('完成'+row['period']['kind']+'：只总结有证据的日期，不声称未覆盖的生活',
            [body],references(body))
        return body,daily
