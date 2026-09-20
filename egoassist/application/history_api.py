"""Authenticated history operations bypass realtime control and semantic routing."""
from aiohttp import web


def mount(app, *, identity, user_key, journal_for, evidence, answers):
    def scope(request, payload):
        user = request[user_key]['id']
        session = payload.get('conversation_id')
        if not isinstance(session, str):
            raise ValueError('conversation_id is required')
        identity.get_context_session(user, session)
        return user, session

    async def ingest(request):
        payload = await request.json(); user, session = scope(request, payload)
        result = evidence.ingest(user, journal_for(user, session), payload.get('events'))
        return web.json_response(result, status=201 if result['status'] == 'indexed' else 202)

    async def query(request):
        payload = await request.json(); user, session = scope(request, payload)
        packet = evidence.query(user, session, payload.get('question'), snapshot_id=payload.get('evidence_snapshot_id'))
        return web.json_response(packet)

    async def ask(request):
        payload = await request.json(); user, session = scope(request, payload)
        question = payload.get('question')
        packet = evidence.query(user, session, question, snapshot_id=payload.get('evidence_snapshot_id'))
        journal = journal_for(user, session)
        result = await answers.answer(journal, question, packet)
        # Do not publish a delayed answer after source deletion or correction of a snapshot.
        evidence.query(user, session, question, snapshot_id=packet['snapshotId'])
        return web.json_response({'result': result, 'evidence': packet}, status=503 if result['status'] == 'unavailable' else 200)

    async def delete(request):
        from egoassist.core.retention import erase_expired
        payload = await request.json(); user, session = scope(request, payload)
        journal = journal_for(user, session)
        target = payload.get('source_id')
        row = next((row for row in journal.entries(types={'source.event'}) if row['source']['id'] == target), None)
        if row is None:
            raise ValueError('unknown source ID')
        erased = erase_expired(journal, {}, source_ids=(row['id'],))
        evidence.index(user).sync()
        return web.json_response({'deleted': True, 'erasedEventCount': len(erased['sourceIds'])})

    @web.middleware
    async def lifecycle(request, handler):
        uid = (request.get(user_key) or {}).get('id')
        maintenance = uid and request.method == 'POST' and request.path in {'/api/privacy/delete', '/api/memory/delete'}
        if maintenance:
            await evidence.pause_user(uid)
        try:
            return await handler(request)
        finally:
            if maintenance:
                evidence.invalidate_user(uid)
                evidence.resume_user(uid)

    app.middlewares.append(lifecycle)
    app.add_routes([web.post('/api/history/import', ingest), web.post('/api/history/query', query),
        web.post('/api/history/ask', ask), web.post('/api/history/delete', delete)])
