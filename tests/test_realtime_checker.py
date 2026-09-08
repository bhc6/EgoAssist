"""Regression checks for the probe's success criteria (local protocol fixtures)."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from scripts.check_realtime import check


async def exercise(events, **kwargs):
    app = web.Application()

    async def login(request):
        return web.json_response({})

    async def create_session(request):
        return web.json_response({'session': {'id': 'probe-test'}}, status=201)

    async def realtime(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json(events[0])
        if kwargs.get('transcript'):
            await ws.receive()
            for event in events[1:]:
                await ws.send_json(event)
        async for _ in ws:
            pass
        return ws

    app.router.add_post('/api/auth/login', login)
    app.router.add_post('/api/sessions', create_session)
    app.router.add_get('/ws/realtime', realtime)
    async with TestServer(app) as server:
        return await check(str(server.make_url('/ws/realtime')), result_timeout=.3, **kwargs)


def test_ready_without_connected_provider_is_failure():
    assert not asyncio.run(exercise([{'type': 'ready', 'lanes': {'dialogue': {'connected': False}}}]))


def test_skill_activation_waits_for_completed_transcript():
    events = [
        {'type': 'ready', 'lanes': {'dialogue': {'connected': True}}},
        {'type': 'state', 'state': {'activeSkill': {'name': 'example'}}},
        {'type': 'assistant_transcript', 'text': 'complete', 'done': True},
    ]
    assert asyncio.run(exercise(events, transcript='hello', expected_skill='example'))
    events[-1]['done'] = False
    assert not asyncio.run(exercise(events, transcript='hello', expected_skill='example'))
