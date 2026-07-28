from egoassist.realtime import RealtimeEpisodeMemory


def test_realtime_episode_memory_persists_summaries_without_raw_media(tmp_path):
    events = []
    memory = RealtimeEpisodeMemory(
        tmp_path / "episodes.json",
        event_sink=lambda event_type, source, payload, *args, **kwargs: events.append((event_type, source, payload)),
    )
    record = memory.remember(
        "用户把洗好的番茄放在砧板旁",
        kind="scene",
        source="proactive-visual-gate",
        skill="kitchen-coach",
        task="tomato-eggs",
        evidence_event_id="event-1",
    )
    assert record["summary"] == "用户把洗好的番茄放在砧板旁"
    assert memory.snapshot()["retainsRawMedia"] is False
    assert events[-1][0] == "episodic.memory.written"

    restored = RealtimeEpisodeMemory(tmp_path / "episodes.json")
    result = restored.retrieve("番茄放在哪里")
    assert result["selected"][0]["episodeId"] == record["id"]
    assert result["candidates"][0]["selected"] is True


def test_realtime_episode_memory_deduplicates_adjacent_identical_events(tmp_path):
    memory = RealtimeEpisodeMemory(tmp_path / "episodes.json")
    for _ in range(2):
        memory.remember(
            "用户说想组装净水器",
            kind="utterance",
            source="input-audio-transcript",
            skill="assembly-coach",
            task="water-filter",
        )
    assert memory.snapshot()["count"] == 1
