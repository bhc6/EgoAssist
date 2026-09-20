import asyncio
import base64
import io
import json
from copy import deepcopy
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from egoassist.realtime.interaction_window import answer_scene_context, device_clock, interaction_vision_model, visual_sequence_content
from egoassist.product.answer_jobs import AnswerJobs, answer_jobs
from test_shared_scene import make_scene, frame


@pytest.mark.parametrize('clock', [None, {}, {'clockId': 'x', 'monotonicMs': float('nan')},
                                  {'clockId': 'x', 'monotonicMs': -1}, {'clockId': 1, 'monotonicMs': 1}])
def test_invalid_capture_clock_falls_back(clock):
    assert device_clock(clock) is None


def test_visual_model_uses_presentation_size_and_respects_override(monkeypatch):
    monkeypatch.delenv('EGOASSIST_INTERACTION_VISION_MODEL', raising=False)
    assert interaction_vision_model(2) == interaction_vision_model(4) == 'qwen3.5-plus'
    assert interaction_vision_model(5) == interaction_vision_model(16) == 'qwen3-vl-plus'
    monkeypatch.setenv('EGOASSIST_INTERACTION_VISION_MODEL', 'custom')
    assert interaction_vision_model(2) == interaction_vision_model(16) == 'custom'


def test_dense_storyboard_keeps_all_sources_and_full_endpoint(tmp_path):
    scene = make_scene(tmp_path)
    refs = []
    for index in range(16):
        frame(scene, (index * 15, 0, 0))
        refs.append({**scene._frame_ref(scene.frames[-1]), 'offsetMs': (index-15)*250})
    content, labels, sheets = visual_sequence_content(scene, refs)
    assert len(sheets) == 2 and len(labels) == 16
    assert list(labels.values()) == [f['sampleId'] for f in refs]
    images = [r['image'] for r in content if 'image' in r]
    assert len(images) == 3
    assert base64.b64decode(images[-1].split(',',1)[1]) == scene.media(refs[-1])
    assert Image.open(io.BytesIO(scene.media(sheets[0]['media']))).size == (1280,416)


def test_answer_projection_keeps_facts_sources_gaps_and_does_not_mutate(tmp_path):
    async def run():
        scene = make_scene(tmp_path)
        frame(scene, 'red');frame(scene, 'blue')
        row = scene.capture('question', explicit=True)
        context = await scene.context('question')
        refs = row['interactionWindow']['frames']
        context['interactionWindow']['gaps'] = [{'durationMs': 2000}]
        context['visual'].update(evidence=[{'description': 'Visible state changed', 'sourceIds': [r['sampleId'] for r in refs]}],
            uncertainties=['Motion between samples is unknown'], rawOutput='redundant',
            frames=refs, presentation={'sourceLabels': {'F01': refs[0]['sampleId']}})
        original = deepcopy(context)
        projected = answer_scene_context(context)
        assert context == original
        assert projected['visual']['evidence'] == original['visual']['evidence']
        assert projected['visual']['uncertainties'] == original['visual']['uncertainties']
        assert projected['interactionWindow']['gaps'] == [{'durationMs': 2000}]
        assert [r['sourceId'] for r in projected['interactionWindow']['frames']] == [r['sampleId'] for r in refs]
        assert 'rawOutput' not in projected['visual'] and 'presentation' not in projected['visual']
        assert len(json.dumps(projected)) < len(json.dumps(original))
        await scene.close()
    asyncio.run(run())


def test_recovered_answer_keeps_same_frozen_sequence(tmp_path):
    async def run():
        scene = make_scene(tmp_path)
        frame(scene, 'red');frame(scene, 'blue')
        row = scene.capture('question', explicit=True)
        context = await scene.context('question')
        manager = AnswerJobs('test')
        manager.register('user', scene.journal, turn_id='question', question='What changed?',
                         instructions='Use the frozen evidence.', context={'sharedScene': context})
        frame(scene, 'green')  # Arrived after the question, cannot enter recovery.
        manager.answer_service.complete = AsyncMock(return_value={'request_id': 'test',
            'output': {'choices': [{'message': {'content': 'Red then blue.'}, 'finish_reason': 'stop'}]}})
        result = await manager._generate(scene.journal, answer_jobs(scene.journal)['question'])
        assert result['frameSequenceEvidence'] == row['interactionWindow']['frames']
        content = manager.answer_service.complete.call_args.args[0][-1]['content']
        supplied = [base64.b64decode(r['image'].split(',',1)[1]) for r in content if 'image' in r]
        assert supplied == [scene.media(f) for f in row['interactionWindow']['frames']]
        assert len(supplied) == 2
        assert result['model'] == interaction_vision_model(2)
        sources = next(json.loads(r['text'])['imageSourceIds'] for r in content if 'imageSourceIds' in r.get('text', ''))
        assert list(sources.values()) == [r['sampleId'] for r in row['interactionWindow']['frames']]
        await manager.close();await scene.close()
    asyncio.run(run())
