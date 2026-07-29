from datetime import datetime, timedelta, timezone
import json

import numpy as np

from egoassist.memory import MemoryItem, TimelineMemory, import_visual_items
from egoassist.memory.timeline import looks_like_question


def remember(memory, text, **overrides):
    values = {
        "user_id": "user-a",
        "case_id": "case-a",
        "scope": "case",
        "kind": "fact",
    }
    values.update(overrides)
    return memory.remember(text, **values)


def test_timeline_persists_and_retrieves_declarative_fact_after_restart(tmp_path):
    path = tmp_path / "timeline.sqlite3"
    memory = TimelineMemory(path)
    phone = remember(
        memory,
        "小李的电话号码是13657352908",
        memory_key="contact:小李:phone",
    )
    remember(memory, "用户把番茄放在砧板旁", kind="scene")

    restored = TimelineMemory(path)
    result = restored.retrieve(
        "你记得关于小李的什么信息",
        user_id="user-a", case_id="case-a", scope="case",
    )

    assert result["selected"][0]["cardId"] == phone["id"]
    assert result["selected"][0]["card"]["text"] == "小李的电话号码是13657352908"
    assert "_embedding" not in result["selected"][0]["card"]


def test_timeline_today_filter_prefers_today_event(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    now = datetime.now(timezone.utc)
    remember(
        memory, "用户今天已经吃过药，服用了维生素D3",
        kind="event", happened_at=now,
    )
    remember(
        memory, "用户昨天吃过药",
        kind="event", happened_at=now - timedelta(days=1),
    )

    result = memory.retrieve(
        "我今天吃过药了吗",
        user_id="user-a", case_id="case-a", scope="case", now=now,
    )

    assert result["selected"][0]["card"]["text"] == "用户今天已经吃过药，服用了维生素D3"
    assert all(item["card"]["text"] != "用户昨天吃过药" for item in result["selected"])


def test_repeated_event_text_keeps_each_occurrence_timestamp(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    now = datetime.now(timezone.utc)
    first = remember(memory, "用户已经服药", kind="event", happened_at=now - timedelta(days=1))
    second = remember(memory, "用户已经服药", kind="event", happened_at=now)

    events = memory.cards(user_id="user-a", case_id="case-a", scope="case")
    assert [item["id"] for item in events] == [second["id"], first["id"]]


def test_timeline_supersedes_mutable_state_but_keeps_history(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    first = remember(
        memory, "维生素D3放在厨房架子上",
        kind="state", memory_key="location:维生素D3",
    )
    second = remember(
        memory, "维生素D3放在书桌右边的架子上",
        kind="state", memory_key="location:维生素D3",
    )

    active = memory.cards(user_id="user-a", case_id="case-a", scope="case")
    history = memory.cards(
        user_id="user-a", case_id="case-a", scope="case",
        statuses=("active", "superseded"),
    )

    assert [item["id"] for item in active] == [second["id"]]
    assert next(item for item in history if item["id"] == first["id"])["status"] == "superseded"
    assert second["supersedes"] == first["id"]


def test_explicit_capture_promotes_next_value_and_is_consumed(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    capture = memory.begin_capture(user_id="user-a", case_id="case-a")

    assert memory.pending_capture(user_id="user-a", case_id="case-a")["id"] == capture["id"]
    consumed = memory.consume_capture(user_id="user-a", case_id="case-a")
    assert consumed["id"] == capture["id"]
    assert memory.pending_capture(user_id="user-a", case_id="case-a") is None


def test_expired_capture_is_not_left_as_pending_state(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    capture = remember(
        memory,
        "Awaiting the user's next memory value",
        kind="capture",
        status="pending",
        happened_at=datetime.now(timezone.utc) - timedelta(minutes=6),
    )

    assert memory.pending_capture(user_id="user-a", case_id="case-a") is None
    history = memory.cards(
        user_id="user-a", case_id="case-a", scope="case", statuses=("consumed",),
    )
    assert history[0]["id"] == capture["id"]


def test_consolidation_moves_old_short_cards_to_long_with_sources(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3", short_term_capacity=4)
    for index in range(6):
        remember(
            memory, f"用户在第{index}次练习后休息",
            kind="event", memory_key="routine:practice-rest", supersede=False,
        )

    result = memory.consolidate(user_id="user-a", case_id="case-a", scope="case")
    long_cards = [
        item for item in memory.cards(user_id="user-a", case_id="case-a", scope="case")
        if item["tier"] == "long"
    ]

    assert result["status"] == "consolidated"
    assert long_cards
    assert len(long_cards[0]["sourceIds"]) >= 2


def test_legacy_import_drops_questions_instead_of_polluting_memory(tmp_path):
    source = tmp_path / "episodes.json"
    source.write_text(json.dumps({"records": [
        {"id": "fact-1", "summary": "小李的电话号码是13657352908"},
        {"id": "question-1", "summary": "小李的电话号码是什么？"},
    ]}, ensure_ascii=False), encoding="utf-8")
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")

    imported = memory.import_legacy_json(
        source, user_id="user-a", case_id="case-a", scope="case", kind="legacy",
    )
    cards = memory.cards(user_id="user-a", case_id="case-a", scope="case")

    assert imported == 1
    assert [item["text"] for item in cards] == ["小李的电话号码是13657352908"]
    assert looks_like_question("我今天吃过药了吗？") is True


def test_participant_and_case_scopes_are_isolated_in_one_store(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    remember(memory, "案例内事件", kind="event")
    remember(
        memory, "用户偏好简短回答", kind="preference",
        scope="participant", memory_key="preference:response-style",
    )

    case_cards = memory.cards(user_id="user-a", case_id="case-a", scope="case")
    participant_cards = memory.cards(user_id="user-a", scope="participant")

    assert [item["text"] for item in case_cards] == ["案例内事件"]
    assert [item["text"] for item in participant_cards] == ["用户偏好简短回答"]


def test_same_memory_key_never_supersedes_across_scope_or_case(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    participant = remember(
        memory, "用户长期把维生素放在随身包里", kind="state",
        scope="participant", memory_key="location:vitamin",
    )
    first_case = remember(
        memory, "本案例中维生素放在厨房架子上", kind="state",
        memory_key="location:vitamin",
    )
    second_case = remember(
        memory, "另一个案例中维生素放在书桌上", kind="state",
        case_id="case-b", memory_key="location:vitamin",
    )

    assert memory.cards(user_id="user-a", scope="participant")[0]["id"] == participant["id"]
    assert memory.cards(user_id="user-a", case_id="case-a", scope="case")[0]["id"] == first_case["id"]
    assert memory.cards(user_id="user-a", case_id="case-b", scope="case")[0]["id"] == second_case["id"]


def test_unrelated_chinese_cards_do_not_match_on_one_shared_character(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    remember(memory, "用户把番茄放在案板旁边", kind="scene")

    result = memory.retrieve(
        "小李的电话号码是多少",
        user_id="user-a", case_id="case-a", scope="case",
    )

    assert result["selected"] == []


def test_visual_memory_adapter_preserves_temporal_and_frame_provenance(tmp_path):
    timeline = TimelineMemory(tmp_path / "timeline.sqlite3")
    items = [MemoryItem(
        id=7,
        t_start=12.5,
        t_end=18.0,
        embedding=np.ones(4, dtype=np.float32),
        caption="用户把蓝色杯子放到书桌右侧",
        keyframe_times=[12.5, 17.5],
        keyframe_meta=[{"source_frame_index": 25}, {"source_frame_index": 35}],
        level="short",
    )]

    cards = import_visual_items(
        timeline, items,
        user_id="benchmark", case_id="video-1", source="egolife:video-1:hash",
    )
    restored = TimelineMemory(tmp_path / "timeline.sqlite3").cards(
        user_id="benchmark", case_id="video-1", scope="case",
    )

    assert len(cards) == len(restored) == 1
    assert restored[0]["kind"] == "scene"
    assert restored[0]["metadata"]["visualEvidence"] is True
    assert restored[0]["metadata"]["videoStartSeconds"] == 12.5
    assert restored[0]["sourceIds"] == [
        "egolife:video-1:hash:memory:7",
        "egolife:video-1:hash:frame:25",
        "egolife:video-1:hash:frame:35",
    ]
