"""Common evidence contract for text, realtime speech and recovered answers."""
import asyncio
import json
import os
import time

import aiohttp

from egoassist.prompts import prompt_text
from egoassist.core.journal import EvidenceUnavailable


def source_event_ids(journal, packet):
    return tuple({source['journalEventId'] for source in packet.get('sources', []) + packet.get('conversation', [])
                  if source.get('sessionId', packet.get('sessionId')) == journal.session_id
                  and source.get('journalEventId')})

def answer_context(packet):
    from egoassist.memory.source_time import annotate
    context = {key: value for key, value in packet.items() if key not in {'audit', 'retrievalMs'}}
    for key in ('sources', 'conversation'):
        context[key] = [annotate(source) for source in packet.get(key, [])]
    return context


def evidence_instructions(packet):
    return prompt_text('evidence_answer/boundary') + '\nEVIDENCE_PACKAGE_JSON:\n' + json.dumps(answer_context(packet), ensure_ascii=False)


def turn_answer_context(question, result, *, clock, runtime, shared_scene):
    packet = result.get('evidencePackage')
    return {'transcript': question, 'retrievalEvidence': result.get('episodicInjection', {}).get('evidence', []),
        'evidencePackage': answer_context(packet) if packet else None, 'clock': clock,
        'runtime': runtime, 'sharedScene': shared_scene, 'timelineWrite': result.get('timelineWrite'),
        'referenceResult': result.get('referenceResult')}


def validate_answer(value, packet):
    if not isinstance(value, dict) or set(value) != {'answer', 'claims', 'unanswered_parts', 'conflicts', 'status'}:
        raise ValueError('answer contract is incomplete')
    if value['status'] not in {'supported', 'partial', 'insufficient'}:
        raise ValueError('invalid answer evidence status')
    if not isinstance(value['answer'], str) or not value['answer'].strip():
        raise ValueError('empty answer')
    allowed = {row['sourceId'] for row in packet['sources'] + packet['conversation']}
    if not isinstance(value['claims'], list):
        raise ValueError('invalid answer claims')
    for claim in value['claims']:
        if not isinstance(claim, dict) or set(claim) != {'text', 'supporting_source_ids'}:
            raise ValueError('invalid answer claim')
        refs = claim['supporting_source_ids']
        if not isinstance(claim['text'], str) or not claim['text'].strip() or not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
            raise ValueError('answer claim has invalid evidence')
    if value['status'] in {'supported', 'partial'} and not value['claims']:
        raise ValueError('supported answer requires cited claims')
    for key in ('unanswered_parts', 'conflicts'):
        if not isinstance(value[key], list) or any(not isinstance(v, str) for v in value[key]):
            raise ValueError('invalid answer uncertainty fields')
    return value


class AnswerService:
    def __init__(self, key, *, model=None, timeout=45):
        self.key = key
        self.model = model or os.getenv('EGOASSIST_ANSWER_MODEL', 'qwen-flash')
        self.timeout = timeout
        self.client = None
        self.slot = asyncio.Semaphore(2)

    async def complete(self, messages, *, model, parameters, endpoint='/services/aigc/text-generation/generation', timeout=None):
        import dashscope
        if self.client is None or self.client.closed:
            self.client = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=self.timeout))
        async with self.client.post(dashscope.base_http_api_url.rstrip('/') + endpoint,
            headers={'Authorization': 'Bearer ' + self.key},
            json={'model': model, 'input': {'messages': messages}, 'parameters': parameters},
            timeout=aiohttp.ClientTimeout(total=timeout or self.timeout)) as response:
            if response.status != 200:
                raise RuntimeError('answer provider HTTP ' + str(response.status))
            return await response.json()

    async def answer(self, journal, question, packet):
        if not self.key:
            raise ValueError('QWEN_API_KEY is not configured')
        instructions = evidence_instructions(packet) + '\n' + prompt_text('evidence_answer/structured')
        messages = [{'role': 'system', 'content': instructions}, {'role': 'user', 'content': question}]
        started = time.perf_counter()
        async with self.slot:
            package = journal.append('evidence.package', {'packet': packet},
                required_event_ids=source_event_ids(journal, packet))
            provenance = {'snapshotId': packet['snapshotId'], 'packageEventId': package['id'],
                'sourceEventIds': list(source_event_ids(journal, packet))}
            for attempt in range(2):
                request = journal.append('evidence.answer.request', {**provenance,
                    'messages': messages, 'model': self.model, 'attempt': attempt + 1}, required_event_ids=(package['id'],))
                try:
                    payload = await self.complete(messages, model=self.model, parameters={
                        'temperature': 0, 'enable_thinking': False, 'max_tokens': 2400,
                        'result_format': 'message', 'response_format': {'type': 'json_object'}})
                    choice = payload['output']['choices'][0]
                    raw = choice['message']['content']
                    journal.append('evidence.answer.response', {**provenance, 'requestEventId': request['id'],
                        'providerRequestId': payload.get('request_id'), 'rawOutput': raw,
                        'usage': payload.get('usage'), 'model': self.model}, parent_id=request['id'],
                        required_event_ids=(package['id'], request['id']))
                    if choice.get('finish_reason') not in {None, 'stop'}:
                        raise ValueError('answer output truncated')
                    result = validate_answer(json.loads(raw), packet)
                    return {**result, 'evidence_snapshot_id': packet['snapshotId'],
                        'validation': {'schema': 'passed', 'citationIds': 'passed', 'entailment': 'not-verified',
                            'statusAuthority': 'model-declared'},
                        'providerRequestId': payload.get('request_id'), 'usage': payload.get('usage'), 'model': self.model,
                        'latencyMs': round((time.perf_counter()-started)*1000, 3)}
                except EvidenceUnavailable:
                    raise
                except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError, KeyError) as exc:
                    journal.append('evidence.answer.failure', {**provenance, 'requestEventId': request['id'],
                        'error': type(exc).__name__ + ': ' + str(exc)[:200], 'attempt': attempt+1}, parent_id=request['id'],
                        required_event_ids=(package['id'], request['id']))
                    if attempt == 0 and isinstance(exc, (aiohttp.ClientConnectionError, TimeoutError)):
                        await asyncio.sleep(.25)
                        continue
                    return {'status': 'unavailable', 'answer': '', 'claims': [], 'unanswered_parts': [question],
                        'conflicts': [], 'error': type(exc).__name__ + ': ' + str(exc)[:200],
                        'evidence_snapshot_id': packet['snapshotId']}

    async def close(self):
        if self.client is not None:
            await self.client.close()
            self.client = None
