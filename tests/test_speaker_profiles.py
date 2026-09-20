import asyncio

import pytest

from egoassist.core.journal import SessionJournal
from egoassist.realtime.scene import SharedScene
from egoassist.realtime.speakers import SpeakerRegistry


def segment(vector, cluster=0, start=0, end=1500):
    return {"embedding": vector, "cluster": cluster, "startMs": start, "endMs": end}


def test_overlapping_mono_segments_neither_enroll_nor_inherit_identity(tmp_path):
    registry = SpeakerRegistry(SessionJournal(tmp_path / "speaker.jsonl"))
    first = registry.match({"segments": [segment([1., 0.])]})
    registry.confirm(first["speakerId"], name="Me", wearer=True)
    result = registry.match({"segments": [segment([1., 0.]), segment([.99, .141], 1, 200, 1400)], "clusterCount": 2})
    assert len(registry.profiles) == 1
    assert all(s["speakerId"] is None and s["method"] == "overlapping-speech" for s in result["segments"])
    assert not result["wearer"]


def test_conflicting_cluster_does_not_create_duplicate_profile(tmp_path):
    registry = SpeakerRegistry(SessionJournal(tmp_path / "speaker.jsonl"))
    result = registry.match({"segments": [segment([1., 0.]), segment([1., 0.], 1, 1600, 3000)], "clusterCount": 2})
    assert len(registry.profiles) == 1
    assert result["segments"][1]["method"] == "cluster-conflict"
    assert result["segments"][1]["speakerId"] is None
    assert result["speakerId"] is None


def test_distinct_sequential_voices_still_enroll_and_repeat_matches(tmp_path):
    registry = SpeakerRegistry(SessionJournal(tmp_path / "speaker.jsonl"))
    result = registry.match({"segments": [segment([1., 0.]), segment([0., 1.], 1, 1600, 3000)], "clusterCount": 2})
    assert len(registry.profiles) == 2
    repeated = registry.match({"segments": [segment([0., 1.])]})
    assert repeated["speakerId"] == result["segments"][1]["speakerId"]
    assert len(registry.profiles) == 2


@pytest.mark.parametrize("status,reason", [("unknown", "audio-too-short"), ("unavailable", "speaker-models-missing")])
def test_unavailable_evidence_does_not_claim_multiple_speakers(tmp_path, status, reason):
    registry = SpeakerRegistry(SessionJournal(tmp_path / "speaker.jsonl"))
    result = registry.match({"status": status, "reason": reason, "segments": []})
    assert result["status"] == status and result["reason"] == reason
    assert not registry.profiles


def test_labels_stable_after_rename_restart_and_turn_revisions(tmp_path):
    journal = SessionJournal(tmp_path / "speaker.jsonl")
    registry = SpeakerRegistry(journal)
    first = registry.match({"segments": [segment([1., 0.])]})
    second = registry.match({"segments": [segment([0., 1.])]})
    assert [p["displayName"] for p in registry.public()] == ["声音 1", "声音 2"]
    for _ in range(3):
        journal.append("scene.turn", {"turnId": "one", "speaker": first})
    registry.confirm(first["speakerId"], name="妈妈")
    scene = SharedScene(journal, "")
    profiles = scene.snapshot()["speakers"]
    assert [p["displayName"] for p in profiles] == ["妈妈", "声音 2"]
    assert [p["turnCount"] for p in profiles] == [1, 0]
    assert profiles[1]["speakerId"] == second["speakerId"]
    assert all("embedding" not in p for p in profiles)
    asyncio.run(scene.close())


def test_merge_keeps_samples_unique_turns_and_is_reversible(tmp_path):
    journal=SessionJournal(tmp_path/'merge.jsonl')
    registry=SpeakerRegistry(journal)
    a=registry.match({'segments':[segment([1.,0.])]})['speakerId']
    b=registry.match({'segments':[segment([0.,1.])]})['speakerId']
    registry.confirm(a,name='妈妈')
    registry.confirm(b,name='我',wearer=True)
    original=deepcopy_profiles(registry)
    journal.append('scene.turn',{'turnId':'same','speaker':{'segments':[{'speakerId':a},{'speakerId':b}]}})
    registry.merge(a,b)
    restored=SpeakerRegistry(journal)
    assert len(restored.public())==1 and restored.public()[0]['displayName']=='我'
    assert restored.match({'segments':[segment([1.,0.])]})['speakerId']==b
    assert restored.match({'segments':[segment([1.,0.])]})['wearer']
    scene=SharedScene(journal,'')
    assert scene.snapshot()['speakers'][0]['turnCount']==1
    assert deepcopy_profiles(restored)==original
    restored.undo_merge(restored.merge_state()['latestMerge']['id'])
    final=SpeakerRegistry(journal)
    assert [p['displayName'] for p in final.public()]==['妈妈','我']
    assert final.match({'segments':[segment([1.,0.])]})['speakerId']==a
    asyncio.run(scene.close())


def deepcopy_profiles(registry):
    from copy import deepcopy
    return deepcopy(registry.profiles)


def test_merge_chain_rejects_cycle_and_out_of_order_undo(tmp_path):
    registry=SpeakerRegistry(SessionJournal(tmp_path/'merge.jsonl'))
    ids=[registry.match({'segments':[segment(v)]})['speakerId'] for v in ([1.,0.,0.],[0.,1.,0.],[0.,0.,1.])]
    registry.merge(ids[0],ids[1]); first=registry.merges[-1]['id']
    registry.merge(ids[1],ids[2])
    with pytest.raises(ValueError): registry.merge(ids[2],ids[0])
    with pytest.raises(ValueError): registry.undo_merge(first)
    assert registry.canonical(ids[0])==ids[2]
    registry.undo_merge(registry.merges[-1]['id']);registry.undo_merge(first)
    assert len(registry.public())==3


def test_legacy_overlap_profiles_are_excluded_from_matching(tmp_path):
    journal=SessionJournal(tmp_path/'legacy.jsonl')
    for sid,vector in [('a',[1.,0.]),('b',[.99,.141])]:
        journal.append('speaker.profile',{'speakerId':sid,'embedding':vector,'name':None,'wearer':False})
    journal.append('scene.turn',{'turnId':'old','speaker':{'segments':[
        {**segment([1.,0.]),'speakerId':'a','method':'new-acoustic-profile'},
        {**segment([.99,.141],1,100,1400),'speakerId':'b','method':'new-acoustic-profile'}]}})
    registry=SpeakerRegistry(journal)
    assert not any(p['matchingEligible'] for p in registry.public())
    assert registry.merge_state()['suggestions']
    assert registry.match({'segments':[segment([1.,0.])]})['speakerId'] not in {'a','b'}
