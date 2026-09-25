"""Read-only conversation history, independent of the bounded live work panel."""


def speaker_attribution(scene, turn_id, *, audio=True):
    """Resolve current, explicitly confirmed profile labels without changing evidence."""
    if not audio:
        return {'kind': 'account', 'label': '你的文字', 'basis': 'account-input'}
    turn = scene.turns.get(turn_id, {}) if scene else {}
    speaker = turn.get('speaker') or {}

    def identify(value):
        status = value.get('status', speaker.get('status', 'unknown'))
        sid = value.get('speakerId')
        registry = scene.speakers if scene else None
        root = registry.canonical(sid) if registry else None
        profile = registry.profiles.get(root, {}) if registry else {}
        unknown = {'kind': 'unknown', 'label': '无法确认', 'basis': value.get('method') or status,
                   'speakerId': root, 'name': profile.get('name')}
        if status == 'pending':
            return {**unknown, 'label': '正在辨认声音'}
        if status in {'overlapping-speech', 'unavailable'} or value.get('method') == 'overlapping-speech':
            return {**unknown, 'label': '无法确认 · ' + ('声音分析暂不可用' if status == 'unavailable' else '同时说话')}
        if not sid or not profile or sid in registry.contaminated:
            return unknown
        if profile.get('identitySource') != 'account-confirmed':
            return unknown
        kind = 'self' if profile.get('wearer') else 'other'
        return {'kind': kind, 'label': ('本人' if kind == 'self' else '他人') + ' · 声音匹配',
                'name': profile.get('name'), 'speakerId': root, 'basis': 'acoustic-match-to-account-confirmed-profile'}

    result = identify(speaker)
    segments = speaker.get('segments') or []
    mixed = (speaker.get('clusterCount', 0) > 1 or len({s.get('speakerId') for s in segments}) > 1)
    if mixed:
        result = {'kind': 'mixed', 'label': '多人或混合语音', 'basis': speaker.get('status')}
    utterances = turn.get('utterances') or []
    if utterances or mixed:
        result['segments'] = [{**identify(part), 'startMs': part.get('startMs'), 'endMs': part.get('endMs'),
            'text': part.get('text'), 'transcriptStatus': part.get('status', 'pending')}
            for part in (utterances or segments)]
    return result


def conversation_page(journal, *, before=None, limit=50, scene=None):
    limit = int(limit)
    if not 1 <= limit <= 200:
        raise ValueError('history limit must be between 1 and 200')

    def initial():
        return {'rows': {}, 'responses': {}}

    def reduce(state, entry):
        kind = entry['type']
        rows = state['rows']
        if kind == 'runtime_event' and entry.get('eventType') == 'response.created':
            origin = (entry.get('payload') or {}).get('origin') or {}
            if entry.get('responseId') and origin.get('turnId'):
                state['responses'][entry['responseId']] = origin['turnId']
        if kind == 'turn_inbox':
            key = 'user:' + entry['turnId']
            rows.setdefault(key, {'id': key, 'role': 'user', 'turnId': entry['turnId'],
                'timestamp': entry['timestamp'], 'text': entry['text'], 'source': entry.get('source'), 'status': 'completed'})
        elif kind == 'message' and (entry.get('message') or {}).get('role') in {'user', 'assistant'}:
            message = entry['message']
            if not message.get('content'):
                return
            turn = (entry.get('metadata') or {}).get('turnId')
            response = entry.get('responseId')
            key = ('user:' + turn if message['role'] == 'user' and turn else
                   'assistant:' + response if message['role'] == 'assistant' and response else 'message:' + entry['id'])
            previous = rows.get(key, {})
            # Do not replace a completed transcript with a late partial event.
            if previous.get('status') == 'completed' and entry.get('status') != 'completed':
                return
            rows[key] = {'id': key, 'role': message['role'], 'turnId': turn,
                'responseId': response, 'timestamp': previous.get('timestamp', entry['timestamp']),
                'text': message['content'], 'status': entry.get('status', 'completed'),
                'source': entry.get('source'), 'sourceEventId': entry['id']}
        elif kind == 'answer.job' and entry.get('status') == 'completed' and entry.get('channel') == 'background-text' and entry.get('text'):
            key = 'background:' + entry['turnId']
            rows[key] = {'id': key, 'role': 'assistant', 'turnId': entry['turnId'],
                'timestamp': rows.get(key, {}).get('timestamp', entry['timestamp']), 'text': entry['text'],
                'status': 'completed', 'source': 'background-text', 'sourceEventId': entry['id']}

    def select(state):
        rows = list(state['rows'].values())  # Stable append order; corrections retain their position.
        end = len(rows)
        if before:
            end = next((i for i, r in enumerate(rows) if r['id'] == before), None)
            if end is None:
                raise ValueError('history cursor is no longer available')
        start = max(0, end - limit)
        selected = [{**row, 'turnId': row.get('turnId') or state['responses'].get(row.get('responseId'))}
                    for row in rows[start:end]]
        for row in selected:
            if row['role'] == 'user':
                row['speakerAttribution'] = speaker_attribution(scene, row.get('turnId'),
                    audio=row.get('source') not in {'explicit-control', 'text', 'account-input'})
        return {'messages': selected, 'total': len(rows),
                'nextBefore': rows[start]['id'] if start else None}

    return journal.project('conversation-history-v1', initial, reduce, select=select)
