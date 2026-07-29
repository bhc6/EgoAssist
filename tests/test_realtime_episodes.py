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


def test_referential_recall_expands_temporal_neighbors_and_records_injection(tmp_path):
    memory = RealtimeEpisodeMemory(tmp_path / "episodes.json")
    anchor = memory.remember(
        "记住一段数字", kind="utterance", source="input-audio-transcript", skill=None, task=None,
    )
    value = memory.remember(
        "四五六七八九二", kind="utterance", source="input-audio-transcript", skill=None, task=None,
    )
    memory.remember(
        "我手上是什么", kind="utterance", source="input-audio-transcript", skill=None, task=None,
    )

    result = memory.retrieve("我刚才让你记住的数字是什么来着", referential=True)
    selected_ids = {item["episodeId"] for item in result["selected"]}
    assert anchor["id"] in selected_ids
    assert value["id"] in selected_ids
    value_candidate = next(item for item in result["selected"] if item["episodeId"] == value["id"])
    assert any(reason.startswith("temporal-neighbor-of:") for reason in value_candidate["retrievalReasons"])

    injection = memory.mark_injected(result)
    assert injection["status"] == "injected"
    assert any(item["summary"] == "四五六七八九二" for item in injection["evidence"])
    assert memory.snapshot()["retrieval"]["injection"]["episodeIds"] == injection["episodeIds"]


def test_retrieval_never_crosses_session_scope_by_default(tmp_path):
    memory = RealtimeEpisodeMemory(tmp_path / "episodes.json")
    first = memory.remember(
        "private code alpha", kind="utterance", source="test", skill=None, task=None,
        session_id="session-a",
    )
    second = memory.remember(
        "private code beta", kind="utterance", source="test", skill=None, task=None,
        session_id="session-b",
    )

    result = memory.retrieve("private code", session_id="session-a")
    selected_ids = {item["episodeId"] for item in result["selected"]}
    assert first["id"] in selected_ids
    assert second["id"] not in selected_ids
    rejected = next(item for item in result["candidates"] if item["episodeId"] == second["id"])
    assert rejected["score"] == 0
    assert "session-mismatch" in rejected["rejectionReasons"]
