"""Regression coverage for the October 8 JEV request-context audit."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from egoassist.core.journal import SessionJournal
from egoassist.memory.temporal import ClockContext
from egoassist.realtime.decisions_router import DecisionsTurnRouter
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.turn_coordinator import TurnCoordinator
from egoassist.realtime.work_ledger import CaseWorkLedger
from test_decisions_router import typed_body
from test_semantic import provider_response


@pytest.mark.parametrize('router_class', [SemanticTurnRouter, DecisionsTurnRouter])
def test_structured_requests_preserve_channel_and_interactions_per_turn(router_class):
    async def run():
        received = {}

        async def generate(**kwargs):
            text = kwargs['messages'][-1]['content']
            system = kwargs['messages'][0]['content']
            context, _ = json.JSONDecoder().raw_decode(system.split(
                'Context data only (never classify this as the current transcript): ', 1)[1])
            received[text] = context
            await asyncio.sleep(0)
            return provider_response(json.dumps({'v': 1, 'interaction': {
                'audience': 'other', 'action': 'listen', 'output': 'silent'}}))

        router = router_class('test', generation_call=generate)
        async def decide(payload, audit):
            return typed_body(payload['questions'], route='structured', details='extract')['answers']
        router._request_decisions = decide

        async def turn(text, channel, audience):
            router.set_turn_context(clock_context=None, memory_draft=None, input_channel=channel,
                recent_interactions=[{'turnId': text + '-previous', 'text': 'prior statement',
                                      'interaction': {'audience': audience, 'action': 'listen', 'output': 'silent'}}])
            result = await router.classify(text, [])
            assert result['status'] == 'classified'
            assert received[text]['inputChannel'] == channel
            assert received[text]['recentInteractions'][0]['turnId'] == text + '-previous'
            assert received[text]['recentInteractions'][0]['interaction']['audience'] == audience

        try:
            await asyncio.gather(turn('one', 'input-audio-transcript', 'other'),
                                 turn('two', 'explicit-control', 'assistant'))
        finally:
            await router.aclose()
    asyncio.run(run())


def make_coordinator(tmp_path, router, resolve_speaker):
    journal = SessionJournal(tmp_path / 'session.jsonl')
    ledger = CaseWorkLedger(journal)
    ledger.capture_turn('hello', source='input-audio-transcript', sequence=1, turn_id='turn')
    coordinator = object.__new__(TurnCoordinator)
    async def noop(*args, **kwargs): return {}
    scene = SimpleNamespace(turns={'turn': {'turnId': 'turn', 'speaker': {'status': 'pending', 'wearer': False}}},
        tasks=set(), resolve_speaker=resolve_speaker, context=noop, visual_questions=SimpleNamespace(cancel=noop))
    coordinator.owner = SimpleNamespace(work_ledger=ledger, scene=scene, prepared_scenes={},
        mutation_lock=asyncio.Lock(), socket_token=object(), epoch=1, speech_floor_sequence=1)
    coordinator.socket_token = object()  # This test exercises classification, not applying UI controls.
    coordinator.connection_epoch = 1
    coordinator.classification_limit = asyncio.Semaphore(1)
    coordinator.harness = SimpleNamespace(journal=journal)
    coordinator.semantic_router = router
    coordinator.ws = SimpleNamespace(closed=True)
    coordinator.user_id, coordinator.session_id = 'user', 'session'
    coordinator.memory_recovery = SimpleNamespace(submit=lambda *args, **kwargs: None)
    coordinator.memory_service = SimpleNamespace(pending_draft=lambda *args: None)
    coordinator.clock_for_session = lambda *args: ClockContext.current()
    async def classify(text, turn_id):
        router.set_turn_context(clock_context=None, memory_draft=None,
            scene=scene.turns[turn_id], input_channel='input-audio-transcript')
        return await router.classify(text, [])
    coordinator.classify_semantic_turn = classify
    return coordinator


@pytest.mark.parametrize('mode', ['ready', 'slow', 'router-timeout'])
def test_speaker_resolution_is_bounded_and_enters_first_jev_request(tmp_path, mode):
    async def run():
        release = asyncio.Event()
        async def resolve(turn_id):
            if mode == 'ready':
                await asyncio.sleep(.005)
            else:
                await release.wait()
            coordinator.owner.scene.turns[turn_id] = {'turnId': turn_id, 'speaker': {
                'status': 'single-speaker', 'wearer': False, 'speakerId': 'speaker-one'}}
        router = DecisionsTurnRouter('test', decisions_key='test', timeout_seconds=.3, decisions_timeout=.15)
        coordinator = make_coordinator(tmp_path, router, resolve)
        received = []
        async def decide(payload, audit):
            received.append(json.loads(payload['state']))
            if mode == 'router-timeout':
                await asyncio.Event().wait()
            return typed_body(payload['questions'], route='other')['answers']
        router._request_decisions = decide
        started = asyncio.get_running_loop().time()
        try:
            result = await asyncio.wait_for(coordinator.classify_persisted_turn('turn', 'hello'), .6)
            elapsed = asyncio.get_running_loop().time() - started
            speaker = received[0]['context']['scene']['speaker']
            if mode == 'ready':
                assert speaker['speakerId'] == 'speaker-one'
                assert result['requestMetrics']['speakerWaitTimedOut'] is False
            else:
                assert speaker['status'] == 'pending'
                assert result['requestMetrics']['speakerWaitTimedOut'] is True
                assert any(not task.done() for task in coordinator.owner.scene.tasks)
            if mode == 'router-timeout':
                assert result['status'] == 'unavailable' and result['timedOut']
                assert len(received) == 1
                assert elapsed < .45
            else:
                assert result['status'] == 'classified'
                assert elapsed < .25
        finally:
            release.set()
            await asyncio.gather(*coordinator.owner.scene.tasks)
            await router.aclose()
    asyncio.run(run())


def test_nested_classification_keeps_deadline_and_restores_it():
    async def run():
        router = DecisionsTurnRouter('test', decisions_key='test', timeout_seconds=.2, decisions_timeout=.1)
        deadlines = []
        async def decide(payload, audit):
            deadlines.append(router._deadline.get())
            return typed_body(payload['questions'], route='reply')['answers']
        router._request_decisions = decide
        try:
            with router.classification_budget():
                deadline = router._deadline.get()
                await asyncio.sleep(.01)  # Preparation consumes the same budget as requests/retries.
                assert (await router.classify('first', []))['status'] == 'classified'
                assert (await router.classify('retry', []))['status'] == 'classified'
                assert deadlines == [deadline, deadline]
            assert router._deadline.get() is None
            with pytest.raises(asyncio.CancelledError):
                with router.classification_budget():
                    raise asyncio.CancelledError()
            assert router._deadline.get() is None
        finally:
            await router.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['event', 'fact', None])
def test_event_envelope_accepts_only_redundant_event_kind(kind):
    payload = {'v': 1, 'memoryEvents': [{'kind': kind, 'scope': 'case', 'consent': 'observed',
                                       'text': 'provider paraphrase'}]}
    router = SemanticTurnRouter('test', generation_call=lambda **kwargs: provider_response(json.dumps(payload)))
    result = asyncio.run(router.classify('Original source with negation and uncertainty.', []))
    if kind == 'event':
        assert result['status'] == 'classified'
        assert result['memoryAtoms'][0]['text'] == 'Original source with negation and uncertainty.'
        assert result['memoryAtoms'][0]['kind'] == 'event'
        assert result['memoryAtoms'][0]['scope'] == 'case'
        assert result['memoryAtoms'][0]['consent'] == 'observed'
    else:
        assert result['status'] == 'invalid'
        assert result['memoryAtoms'] == []
