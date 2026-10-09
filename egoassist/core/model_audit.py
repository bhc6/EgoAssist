"""Shared recording and read-only views of model inputs and outputs.

Only explicit call/request/response identifiers join records. Missing historical
context is reported, never reconstructed using current prompts or nearby calls.
"""
from copy import deepcopy


PAIRS = ('scene', 'scene.question', 'scene.transcription', 'scene.history',
         'answer', 'evidence.answer', 'long_event', 'insight', 'tool')


def record_request(journal, request, body):
    """Persist the exact JSON body at the sending boundary, without headers."""
    journal.append('model.request.body', {'requestEventId': request['id'], 'sourceKind': request['type'], 'body': body}, parent_id=request['id'], required_event_ids=(request['id'],))
    return body


def record_output(journal, request, payload):
    journal.append('model.response.body', {'requestEventId': request['id'], 'sourceKind': request['type'], 'body': payload}, parent_id=request['id'], required_event_ids=(request['id'],))


def has_output(data):
    return any(data.get(key) is not None for key in ('output', 'rawOutput', 'text', 'description', 'body', 'message'))


def catalogue(journal):
    def initial():
        return {'calls': {}, 'providers': {}, 'requests': {}, 'responses': {}, 'sequence': {}, 'order': 0}

    def reduce(state, row):
        kind = row.get('eventType') or row['type']
        state['order'] += 1
        data = row.get('payload', row)
        calls = state['calls']

        def add(identifier, model, stage, *, input_ids=(), output_ids=(), status='unrecorded', selector=None, input_status='recorded'):
            calls[identifier] = {'id': identifier, 'model': model or 'unknown', 'stage': stage,
                'timestamp': row['timestamp'], 'updatedAt': row['timestamp'], 'status': status, 'order': state['order'],
                'turnId': data.get('turnId') or (data.get('origin') or {}).get('turnId'),
                'responseId': data.get('responseId') or row.get('responseId'),
                'inputEventIds': list(input_ids), 'outputEventIds': list(output_ids),
                'inputStatus': input_status if input_ids else 'missing',
                'outputStatus': 'recorded' if output_ids else 'missing', 'selector': selector}
            return calls[identifier]

        def finish(call, status=None, output_status='recorded'):
            call['outputEventIds'].append(row['id'])
            call.update(updatedAt=row['timestamp'], status=status or data.get('status') or 'recorded', outputStatus=output_status)
            for key in ('error', 'latencyMs', 'usage', 'requestId', 'outputFormat'):
                if key in data: call[key] = data[key]

        if kind in {'control_decision', 'memory.recovery.result', 'interaction.classification_retry'} and isinstance(data.get('decision'), dict):
            decision = data['decision']; metrics = decision.get('requestMetrics') or {}
            for name in ('decisions', 'structured', 'taskControlCheck', 'cancellationCheck'):
                stage = metrics if name == 'structured' else metrics.get(name)
                if not isinstance(stage, dict) or (name == 'structured' and not stage.get('messages')):
                    continue
                output = decision.get('rawOutput') if name == 'structured' else stage.get('rawResponse', stage.get('rawOutput', stage.get('answers')))
                context = stage.get('messages') if name == 'structured' else stage.get('request', stage.get('messages'))
                call = add(row['id'] + ':' + name, stage.get('resolvedModel') or stage.get('model') or decision.get('model'),
                    'memory.' + name if kind.startswith('memory.') else name,
                    input_ids=[row['id']] if context is not None else [], output_ids=[row['id']] if output is not None else [],
                    status=decision.get('status', 'unrecorded') if name == 'structured' else
                           'failed' if stage.get('error') or stage.get('providerError') else 'recorded' if output is not None else 'unavailable',
                    selector=name)
                call['latencyMs'] = decision.get('latencyMs') if name == 'structured' else stage.get('latencyMs')
                call['error'] = stage.get('error') or stage.get('providerError') or (decision.get('error') if output is None else None)
            if not metrics:
                add(row['id'], decision.get('model'), kind, output_ids=[row['id']], status=decision.get('status'), selector='legacy-decision')
        elif kind == 'provider.model.request':
            call = add(row['id'], data.get('model'), data.get('stage'), input_ids=[row['id']], status='awaiting-output')
            state['providers'][data['callId']] = call['id']
        elif kind in {'provider.model.input', 'provider.model.response'}:
            identifier = state['providers'].get(data.get('callId'))
            if identifier is None:
                add(row['id'], data.get('model'), data.get('stage'), output_ids=[row['id']], status=data.get('status', 'unrecorded'))
            elif kind.endswith('.input'):
                calls[identifier]['inputEventIds'].append(row['id'])
                calls[identifier]['updatedAt'] = row['timestamp']
            else:
                finish(calls[identifier], output_status='recorded' if data.get('output') is not None else 'missing')
        elif kind in {prefix + '.request' for prefix in PAIRS}:
            prefix = kind.rsplit('.', 1)[0]
            add(row['id'], data.get('model'), prefix, input_ids=[row['id']], status='awaiting-output',
                input_status='partial')
            state['requests'][row['id']] = row['id']
        elif kind in {'model.request.body', 'model.response.body'}:
            identifier = state['requests'].get(data.get('requestEventId'))
            if identifier:
                call = calls[identifier]
                if kind == 'model.request.body':
                    call['inputEventIds'].append(row['id'])
                    call['inputStatus'] = 'recorded'
                else:
                    finish(call, 'received')
        elif kind in {'tool.selection', 'tool.selection.failed'} or kind in {prefix + suffix for prefix in PAIRS for suffix in ('.result', '.response', '.cancelled', '.failure')}:
            candidates = [data.get('requestEventId'), data.get('requestId')]
            # These producers explicitly persisted their request as parent.
            # scene.result/transcription historically used chronological parents.
            if kind.startswith(('scene.question.', 'scene.history.', 'insight.', 'answer.', 'evidence.answer.')):
                candidates.append(row.get('parentId'))
            identifier = next((state['requests'][ref] for ref in candidates if ref in state['requests']), None)
            if identifier:
                call = calls[identifier]
                finish(call, 'cancelled' if kind.endswith('.cancelled') else 'failed' if kind.endswith(('.failure', '.failed')) else None,
                       'recorded' if has_output(data) or call['outputStatus'] == 'recorded' else 'missing')
            else:
                call = add(row['id'], data.get('model'), kind, output_ids=[row['id']], status=data.get('status', 'recorded'))
                if not has_output(data): call['outputStatus'] = 'missing'
        elif kind == 'long_event.job' and data.get('requestId') in state['requests']:
            call = calls[state['requests'][data['requestId']]]
            if data.get('status') in {'failed', 'completed', 'queued'}:
                finish(call, data['status'], call['outputStatus'])
        elif kind == 'response.dispatched' and data.get('backend') not in {'text-and-tts', 'visual-answer-and-tts'}:
            # Native provider session state cannot be inferred from a dispatch.
            model = data.get('model') or row.get('model')
            if model and 'omni' in model.lower():
                call = add(row['id'], model, 'native-realtime', input_ids=[row['id']], status='awaiting-output', input_status='partial')
                state['sequence'][(data.get('sequence'), call['turnId'])] = call['id']
        elif kind == 'response.created':
            identifier = state['sequence'].get((data.get('sequence'), (data.get('origin') or {}).get('turnId')))
            if identifier and row.get('responseId'):
                state['responses'][row['responseId']] = identifier
                calls[identifier]['responseId'] = row['responseId']
        elif kind == 'message' and (data.get('message') or {}).get('role') == 'assistant':
            identifier = state['responses'].get(data.get('responseId'))
            if identifier:
                finish(calls[identifier], data.get('status'), 'partial')
        elif kind == 'response.finished':
            identifier = state['responses'].get(row.get('responseId'))
            if identifier:
                calls[identifier].update(status=data.get('status', 'unrecorded'), updatedAt=row['timestamp'])

    return journal.project('model-call-audit-v1', initial, reduce, select=lambda state: deepcopy(state['calls']))


def page(journal, *, model=None, before=None, limit=20):
    limit = int(limit)
    if not 1 <= limit <= 100:
        raise ValueError('model call limit must be between 1 and 100')
    calls = list(catalogue(journal).values())
    latest = {}
    for call in sorted(calls, key=lambda c: c['order']):
        latest[call['model']] = call
    ordered = sorted((c for c in calls if not model or c['model'] == model), key=lambda c: c['order'], reverse=True)
    if before:
        position = next((i for i, c in enumerate(ordered) if c['id'] == before), None)
        if position is None:
            raise ValueError('unknown model call cursor')
        ordered = ordered[position + 1:]
    selected = ordered[:limit]
    return {'models': sorted(latest.values(), key=lambda c: c['order'], reverse=True), 'calls': selected, 'totalCalls': len(calls),
            'erasureVersion': len(journal.entries(types={'retention.deleted'})),
            'nextBefore': selected[-1]['id'] if len(ordered) > limit else None}


def detail(journal, identifier):
    call = catalogue(journal).get(identifier)
    if call is None:
        raise ValueError('model call not found in this session')
    ids = list(dict.fromkeys(call['inputEventIds'] + call['outputEventIds']))
    rows = journal.entries_by_id(ids)
    inputs, outputs = [], []
    for event_id in ids:
        row = rows.get(event_id)
        if row is None:
            continue
        data = row.get('payload', row); selector = call['selector']
        if selector and selector != 'legacy-decision':
            decision = data['decision']; metrics = decision.get('requestMetrics') or {}
            stage = metrics if selector == 'structured' else metrics[selector]
            request = {'messages': stage['messages'], 'parameters': stage.get('parameters')} if selector == 'structured' else stage.get('request', {'messages': stage.get('messages')})
            output = decision.get('rawOutput') if selector == 'structured' else stage.get('rawResponse', stage.get('rawOutput', stage.get('answers')))
            if event_id in call['inputEventIds']: inputs.append({'eventId': event_id, 'content': request})
            if event_id in call['outputEventIds']: outputs.append({'eventId': event_id, 'content': output})
        else:
            if event_id in call['inputEventIds']:
                inputs.append({'eventId': event_id, 'content': data.get('body', data.get('request', data))})
            if event_id in call['outputEventIds']:
                outputs.append({'eventId': event_id, 'content': data.get('body', data.get('output', data))})
    return {**call, 'inputs': inputs, 'outputs': outputs,
            'claim': 'Recorded application-side inputs and received outputs; not hidden model state or proof of correctness.'}
