"""Revisit bounded, source-linked historical frames for a paused video question."""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
from datetime import datetime
import json
import math
import os
import re
import time

import aiohttp

from egoassist.prompts import prompt_text


def terms(text):
    parts = re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+', text.casefold())
    return [token for part in parts for token in
            ([part] if part.isascii() or len(part) == 1 else
             [part[i:i + 2] for i in range(len(part) - 1)])]


def select_history(rows, question, *, before, limit=24):
    """Rank observations, reserving chronological coverage for caption omissions.

    Both capture and commit timestamps must precede the question. Images remain
    hypotheses, not identity claims. Selection never uses QA labels or targets.
    """
    if limit <= 0:
        return []
    boundary = datetime.fromisoformat(before)
    eligible = []
    seen = set()
    for row in rows:
        e = row.get('evidence') or {}
        ref = e.get('media') or {}
        if row.get('status') != 'accepted' or not ref.get('sha256'):
            continue
        try:
            capture, recorded = datetime.fromisoformat(e['capturedAt']), datetime.fromisoformat(row['timestamp'])
            if capture > boundary or recorded > boundary:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if ref['sha256'] in seen:
            continue
        seen.add(ref['sha256'])
        eligible.append({'sourceId': row['id'], 'frame': ref, 'capturedAt': e['capturedAt'],
                         'recordedAt': row['timestamp'], 'visibleEvidence': e.get('visibleEvidence', []),
                         'uncertainties': e.get('uncertainties', [])})
    eligible.sort(key=lambda r: (datetime.fromisoformat(r['capturedAt']), r['sourceId']))
    docs = [Counter(terms(' '.join(r['visibleEvidence']))) for r in eligible]
    counts = Counter(t for doc in docs for t in doc)
    query = set(terms(question));n = len(docs)
    average = sum(sum(d.values()) for d in docs) / max(1, n)
    scores = []
    for doc in docs:
        norm = 1.2 * (.25 + .75 * sum(doc.values()) / max(1, average))
        scores.append(sum(math.log1p((n-counts[t]+.5)/(counts[t]+.5))*doc[t]*2.2/(doc[t]+norm)
                          for t in query if doc[t]))
    # Broad coverage is particularly useful when the generic caption omitted a
    # small object that a question-specific visual pass can still inspect.
    coverage = min(max(1, limit // 2), limit, n)
    indices = {round(i*(n-1)/max(1, coverage-1)) for i in range(coverage)}
    ranked = sorted(range(n), key=lambda i: (-scores[i], eligible[i]['sourceId']))
    for i in ranked:
        if len(indices) >= max(0, limit):
            break
        indices.add(i)
    return [{**eligible[i], 'retrievalScore': round(scores[i], 5)} for i in sorted(indices)]


def validate_evidence(parsed, source_ids):
    """Reject malformed claims before they enter the answer context."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get('evidence'), list):
        raise ValueError('historical visual response requires an evidence list')
    uncertainties = parsed.get('uncertainties')
    if not isinstance(uncertainties, list) or any(not isinstance(x, str) for x in uncertainties):
        raise ValueError('historical visual uncertainties must be strings')
    for fact in parsed['evidence']:
        if not isinstance(fact, dict):
            raise ValueError('historical visual evidence must be an object')
        refs = fact.get('sourceIds')
        if (not isinstance(fact.get('description'), str) or not fact['description'].strip()
                or not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) or ref not in source_ids for ref in refs)):
            raise ValueError('historical visual evidence lacks valid source references')
    return parsed['evidence'], uncertainties


class VisualHistory:
    def __init__(self, scene):
        self.scene = scene
        self.limit = asyncio.Semaphore(1)

    async def observe(self, turn_id, question):
        row = self.scene.turns[turn_id]
        rows = self.scene.journal.entries(types={'ambient_observation'}, tail=4096)
        selected = select_history(rows, question, before=row['timestamp'])
        if not selected:
            return {'status': 'no-evidence', 'scope': 'historical-frames', 'sources': []}
        model = os.getenv('EGOASSIST_VISUAL_HISTORY_MODEL', 'qwen3.5-plus')
        prompt = prompt_text('scene/history')
        result = {'turnId': turn_id, 'scope': 'historical-frames', 'model': model,
                  'sources': selected, 'question': question, 'coverage': 'bounded-samples-not-exhaustive'}
        request = self.scene.journal.append('scene.history.request', {**result, 'prompt': prompt})
        started = time.perf_counter()
        try:
            import dashscope
            content = []
            for item in selected:
                # Capture receipt time is not the depicted video's clock. Keep
                # it in the audit, not beside the image as an event-time label.
                content.extend([{'text': json.dumps({'sourceId': item['sourceId']})},
                                {'image': 'data:image/jpeg;base64,' + base64.b64encode(self.scene.media(item['frame'])).decode()}])
            content.append({'text': question})
            async with asyncio.timeout(35), self.limit:
                async with self.scene.http().post(dashscope.base_http_api_url.rstrip('/') + '/services/aigc/multimodal-generation/generation',
                    headers={'Authorization': 'Bearer ' + self.scene.api_key}, timeout=aiohttp.ClientTimeout(total=30),
                    json={'model': model, 'input': {'messages': [
                        {'role': 'system', 'content': [{'text': prompt}]}, {'role': 'user', 'content': content}]},
                        'parameters': {'temperature': 0, 'enable_thinking': False, 'max_tokens': 1000,
                                       'response_format': {'type': 'json_object'}}}) as response:
                    response.raise_for_status();payload = await response.json()
            choice = payload['output']['choices'][0];value = choice['message']['content']
            raw = value if isinstance(value, str) else ''.join(v.get('text', '') for v in value)
            parsed = json.loads(raw)
            source_ids = {s['sourceId'] for s in selected}
            if choice.get('finish_reason') not in {None, 'stop'}:
                raise ValueError('incomplete historical visual response')
            facts, uncertainties = validate_evidence(parsed, source_ids)
            result.update(status='observed', evidence=facts, uncertainties=uncertainties,
                          requestId=payload.get('request_id'), usage=payload.get('usage'), rawOutput=raw)
        except asyncio.CancelledError:
            self.scene.journal.append('scene.history.cancelled', {'turnId': turn_id}, parent_id=request['id'])
            raise
        except Exception as exc:
            result.update(status='unavailable', error=type(exc).__name__ + ': ' + str(exc)[:160])
        result['latencyMs'] = round((time.perf_counter()-started)*1000, 1)
        self.scene.journal.append('scene.history.result', result, parent_id=request['id'])
        return result
