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


def test_referential_recall_uses_declarative_card_without_temporal_guessing(tmp_path):
    memory = RealtimeEpisodeMemory(tmp_path / "episodes.json")
    value = memory.remember(
        "用户要求记住的号码是四五六七八九二",
        kind="fact", source="input-audio-transcript", skill=None, task=None,
        memory_key="remembered-number",
    )
    ignored_question = memory.remember(
        "我手上是什么？", kind="utterance", source="input-audio-transcript", skill=None, task=None,
    )
    mislabeled_question = memory.remember(
        "小李的电话是什么？", kind="fact", source="research-api", skill=None, task=None,
    )

    result = memory.retrieve("我刚才让你记住的数字是什么来着", referential=True)
    selected_ids = {item["episodeId"] for item in result["selected"]}
    assert value["id"] in selected_ids
    assert ignored_question is None
    assert mislabeled_question is None
    assert all(
        not any(reason.startswith("temporal-neighbor-of:") for reason in item["retrievalReasons"])
        for item in result["candidates"]
    )

    injection = memory.mark_injected(result)
    assert injection["status"] == "injected"
    assert any(item["summary"] == "用户要求记住的号码是四五六七八九二" for item in injection["evidence"])
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
    assert second["id"] not in {item["episodeId"] for item in result["candidates"]}
    assert result["scope"]["memoryScope"] == "case"
