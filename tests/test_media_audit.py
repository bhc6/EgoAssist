from egoassist.realtime.media_audit import MediaAuditAccumulator


def test_media_audit_aggregates_exact_totals_with_bounded_event_count():
    events = []
    audit = MediaAuditAccumulator(
        lambda event, source, payload: events.append((event, source, payload)),
        max_events=100,
        clock=lambda: 10.0,
    )

    for index in range(1000):
        audit.record_audio(320, video_flushed=index % 10 == 0)
    for _ in range(500):
        audit.record_video(2048)
    audit.flush()

    audio = [payload for event, _, payload in events if event == "media.audio.batch"]
    video = [payload for event, _, payload in events if event == "media.video.batch"]
    assert sum(item["chunks"] for item in audio) == 1000
    assert sum(item["base64Bytes"] for item in audio) == 320000
    assert sum(item["videoFlushes"] for item in audio) == 100
    assert sum(item["frames"] for item in video) == 500
    assert sum(item["base64Bytes"] for item in video) == 1024000
    assert len(events) <= 15
    assert all("audio" not in payload and "image" not in payload for _, _, payload in events)


def test_media_audit_flushes_partial_batch_at_session_end():
    events = []
    audit = MediaAuditAccumulator(
        lambda event, source, payload: events.append((event, payload)),
        max_events=100,
        clock=lambda: 20.0,
    )
    audit.record_audio(12, video_flushed=False)
    audit.record_video(34)

    assert audit.flush() == 2
    assert audit.flush() == 0
    assert events == [
        ("media.audio.batch", {"chunks": 1, "base64Bytes": 12, "videoFlushes": 0}),
        ("media.video.batch", {"frames": 1, "base64Bytes": 34, "policy": "latest-only"}),
    ]
