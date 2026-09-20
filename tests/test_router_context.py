import copy

from egoassist.realtime.router_context import project_context


def test_router_projection_keeps_identity_and_time_without_media_metadata():
    context = {"previousTurn": {"turnId": "old", "text": "original question", "status": "interrupted", "idempotencyKey": "opaque"},
               "previousAnswer": {"turnId": "older", "text": "keep target"},
               "clock": {"now": "2026-09-09"}, "playback": {"turnId": "old"},
               "outputPermission": {"quiet": True, "playback": {"turnId": "old"}},
               "scene": {"turnId": "new", "speaker": {"status": "mixed-or-unknown", "wearer": False, "omittedSegments": 2},
                         "audio": {"sha256": "audio-hash"}, "segment": {"audioStartMs": 800, "audioEndMs": 1800},
                         "frame": {"frameId": "f", "receivedAt": "then", "sha256": "frame-hash"},
                         "avAssociation": {"status": "unlinked"},
                         "synchronizedFrames": [{"sha256": "frame-hash", "syncBasis": "server-receipt", "syncAccuracy": "unmeasured"}],
                         "utterances": [{"text": "heard", "speakerId": "speaker-1", "status": "transcribed", "audio": {"sha256": "hash"}}]}}
    original = copy.deepcopy(context)
    projected = project_context(context)
    assert projected["scene"]["speaker"] == context["scene"]["speaker"]
    assert projected["scene"]["segment"] == context["scene"]["segment"]
    assert projected["scene"]["avAssociation"] == {"status": "unlinked"}
    assert projected["scene"]["synchronizedFrames"]["syncAccuracy"] == ["unmeasured"]
    assert projected["scene"]["audioAvailable"] is True
    assert projected["previousTurn"]["turnId"] == "old" and projected["previousAnswer"] == context["previousAnswer"]
    assert projected["outputPermission"] == {"quiet": True}
    assert "sha256" not in str(projected)
    assert context == original
    assert project_context(projected) == projected


def test_unverified_voice_needs_explicit_addressee_before_answering():
    from egoassist.realtime.scene import restrict_unverified_speaker

    implicit = restrict_unverified_speaker({"status": "classified", "interaction": None})
    assert implicit["interaction"] == {"audience": "uncertain", "action": "uncertain", "output": "silent"}
    explicit = restrict_unverified_speaker({"interaction": {"audience": "assistant", "action": "reply", "output": "speech"}})
    assert explicit["interaction"]["action"] == "reply"
    cancel = restrict_unverified_speaker({"interaction": {"audience": "assistant", "action": "cancel", "output": "silent"}})
    assert cancel["interaction"]["action"] == "cancel"
