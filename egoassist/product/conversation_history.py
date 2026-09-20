"""Read-only conversation history, independent of the bounded live work panel."""


def conversation_page(journal, *, before=None, limit=50):
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
        return {'messages': selected, 'total': len(rows),
                'nextBefore': rows[start]['id'] if start else None}

    return journal.project('conversation-history-v1', initial, reduce, select=select)
