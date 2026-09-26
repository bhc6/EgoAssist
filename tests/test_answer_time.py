import copy
import json

from egoassist.product.answer_time import localize_evidence, time_instructions
from egoassist.product.answer_service import evidence_instructions, turn_answer_context


def test_model_evidence_localizes_nested_observations_without_mutating_source():
    clock={'timezone':'Asia/Shanghai','utcOffsetMinutes':480}
    original={'sourceAt':'2026-09-26T06:19:50.369425+00:00', 'timezone':None,
              'text':json.dumps({'evidence':{'capturedAt':'2026-09-26T06:19:50.369425Z'},
                                 'text':'用户原话 06:19，不应改写'}),
              'eventTime':'未知', 'capturedAt':'2026-09-26T06:19:50'}
    before=copy.deepcopy(original)
    value=localize_evidence(original,clock)
    assert value['sourceAt']=='2026-09-26T14:19:50.369425+08:00'
    nested=json.loads(value['text'])
    assert nested['evidence']['capturedAt']==value['sourceAt']
    assert nested['text']=='用户原话 06:19，不应改写'
    assert value['capturedAt']==original['capturedAt'] and value['timezone'] is None
    assert original==before
    assert localize_evidence(value,clock)==value  # no second offset application


def test_day_boundary_dst_and_unknown_timezone():
    source={'capturedAt':'2026-09-25T18:30:00Z'}
    assert localize_evidence(source,{'timezone':'Asia/Shanghai'})['capturedAt']=='2026-09-26T02:30:00+08:00'
    for stamp,expected in [('2026-01-01T12:00:00Z','2026-01-01T07:00:00-05:00'),
                           ('2026-07-01T12:00:00Z','2026-07-01T08:00:00-04:00')]:
        assert localize_evidence({'capturedAt':stamp},{'timezone':'America/New_York'})['capturedAt']==expected
    assert localize_evidence(source,None)==source
    assert localize_evidence(source,{'timezone':'unknown'})==source
    assert localize_evidence(source,{'timezone':'unknown','utcOffsetMinutes':480})['capturedAt'].endswith('+08:00')


def test_answer_channels_present_same_local_time_and_preserve_clock_authority():
    clock={'timezone':'Asia/Shanghai','nowUtc':'2026-09-26T06:59:34Z','localNow':'2026-09-26T14:59:34+08:00'}
    source={'sourceId':'s1','text':json.dumps({'capturedAt':'2026-09-26T06:19:50Z'}),
            'sourceAt':'2026-09-26T06:19:50Z'}
    packet={'sources':[source],'conversation':[]}
    context=turn_answer_context('几点',{'evidencePackage':packet},clock=clock,runtime={},shared_scene={'capturedAt':source['sourceAt']})
    prompt=evidence_instructions(packet,clock=clock)
    assert '14:19:50+08:00' in prompt and '06:19:50' not in prompt
    assert '14:19:50+08:00' in json.dumps(context)
    assert context['clock']['nowUtc']==clock['nowUtc']
    assert 'Asia/Shanghai' in time_instructions(clock)
