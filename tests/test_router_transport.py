"""Compact protocol and cancellation of real local HTTP requests (no cloud calls)."""
import asyncio
import json

import dashscope
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from egoassist.realtime.semantic import SemanticTurnRouter


def test_compact_decision_preserves_validation():
    async def call(**kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]))

    router = SemanticTurnRouter('test', generation_call=call)
    for payload in ({'v': True}, {'v': 2}, {'v': 1, 'explanation': 'extra'},
                    {'v': 1, 'speechControl': 'other'}, {}):
        assert asyncio.run(router.classify('hello', []))['status'] == 'invalid'
    payload = {'v': 1}
    result = asyncio.run(router.classify('hello', []))
    assert result['status'] == 'classified'
    assert result['memoryKind'] is None
    payload = {'v': 1, 'memoryQuerySpec': {'subject': 'bike', 'predicate': 'location'}}
    result = asyncio.run(router.classify('where is my bike?', []))
    assert result['memoryQuery'] == 'referential'
    assert result['memoryKind'] is None


def test_http_timeout_and_cancellation_release_connections(monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        transports = []
        app = web.Application()

        async def generate(request):
            body = await request.json()
            assert body['parameters']['enable_thinking'] is False
            assert body['input']['messages'][-1]['role'] == 'user'
            text = body['input']['messages'][-1]['content']
            if text == 'slow':
                transports.append(request.transport)
                entered.set()
                await release.wait()
            return web.json_response({'output': {'choices': [{'message': {'content': '{"v":1}'}}]},
                                      'usage': {'input_tokens': 1, 'output_tokens': 5}})

        app.router.add_post('/services/aigc/text-generation/generation', generate)
        async with TestServer(app) as server:
            monkeypatch.setattr(dashscope, 'base_http_api_url', str(server.make_url('/')).rstrip('/'))
            router = SemanticTurnRouter('test', timeout_seconds=.5)
            try:
                result = await router.classify('slow', [])
                assert entered.is_set()
                assert result['status'] == 'unavailable' and result['timedOut']
                assert 'TimeoutError' in result['error']
                assert not router._http_session.connector._acquired
                session = router._http_session
                result = await router.classify('fast', [])
                assert result['status'] == 'classified'
                assert result['requestMetrics']['usage']['output_tokens'] == 5
                assert router._http_session is session
                entered.clear()
                task = asyncio.create_task(router.classify('slow', []))
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not session.connector._acquired
                await asyncio.sleep(.01)
                assert all(transport.is_closing() for transport in transports)
            finally:
                release.set()
                await router.aclose()
            assert session.closed
    asyncio.run(scenario())
