"""Real-model order, unchanged-frame and unobserved-detail checks on saved images.

Expectations are evaluation-only and never supplied to the model. A successful
transport/citation check is not an automatic semantic accuracy score.
"""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from egoassist._env import load_env
from egoassist.core.journal import SessionJournal
from egoassist.realtime.scene import SharedScene
from egoassist.realtime.interaction_window import answer_scene_context


async def verify(source, output):
    output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in source.read_text(encoding='utf8').splitlines()]
    samples = [r for r in rows if r['type'] == 'ambient_observation' and
               '2026-09-18T04:48:10' <= (r.get('evidence') or {}).get('capturedAt', '') <= '2026-09-18T04:48:19']
    assert len(samples) == 2
    images = [(source.parent/'media'/source.stem/r['evidence']['media']['assetId']).read_bytes() for r in samples]
    cases = [
        ('forward', [0, 1], '刚才他的手有什么变化？只说画面能确认的。',
         'Initially near chin, later no longer at chin. Hidden final hand position is unknown.'),
        ('reverse', [1, 0], '刚才他的手有什么变化？只说画面能确认的。',
         'Initially not at chin, finally near chin. Must reverse the forward comparison.'),
        ('unchanged', [1, 1], '刚才他的手有什么变化？只说画面能确认的。',
         'Identical endpoint pixels do not support a hand movement.'),
        ('unseen', [0, 1], '他脚上的鞋子是什么颜色？',
         'Shoes outside both frames; color unknown.'),
    ]
    report = {'chainPassed': False, 'semanticReview': 'manual-required', 'realModel': True,
              'realSavedImages': True, 'physicalHardwareMeasured': False,
              'sourceJournal': str(source.resolve()), 'cases': []}
    try:
        for name, order, question, expected in cases:
            scene = SharedScene(SessionJournal(output/(name+'.jsonl')), os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY'))
            try:
                for index, source_index in enumerate(order):
                    scene.append_frame(base64.b64encode(images[source_index]).decode(),
                        {'clockId': name, 'monotonicMs': 1000+index*1000}, describe=False)
                scene.capture(name, explicit=True, metadata={'clockId': name, 'monotonicMs': 2050})
                context = await scene.context(name, describe_frame=True, question=question)
                projected = answer_scene_context(context)
                result = {'case': name, 'imageOrder': order, 'question': question,
                          'expectedForReviewOnly': expected, 'context': context,
                          'answerContextChars': {'before': len(json.dumps(context, ensure_ascii=False)),
                                                 'after': len(json.dumps(projected, ensure_ascii=False))}}
                report['cases'].append(result)
                print(json.dumps({'case': name, 'visual': {key: context['visual'].get(key) for key in
                    ('status', 'model', 'description', 'uncertainties', 'latencyMs', 'error')},
                    'answerContextChars': result['answerContextChars']}, ensure_ascii=True), flush=True)
            finally:
                await scene.close()
        report['chainPassed'] = all(c['context']['visual']['status'] == 'observed' for c in report['cases'])
    finally:
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return report['chainPassed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    load_env()
    raise SystemExit(0 if asyncio.run(verify(args.journal, args.output)) else 1)
