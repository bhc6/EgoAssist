from datetime import datetime, timedelta, timezone
import json
import sqlite3

import numpy as np

from egoassist.memory import ClockContext, MemoryItem, TimelineMemory, import_visual_items, resolve_time_semantics
from egoassist.memory.timeline import (
    DEFAULT_RETRIEVAL_MIN_SCORE,
    looks_like_question,
    memory_key_for,
)


def remember(memory, text, **overrides):
    values = {
        "user_id": "user-a",
        "case_id": "case-a",
        "scope": "case",
        "kind": "fact",
    }
    values.update(overrides)
    return memory.remember(text, **values)


def test_structured_lookup_obeys_kind_allowlist_and_zero_score_constraints(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.sqlite3")
    remember(memory, "相机位置是抽屉", kind="capture", subject="相机", predicate="位置", value="抽屉")
    remember(memory, "钥匙位置是门口", subject="钥匙", predicate="位置", value="门口")
    for kinds in ([], ["capture"], ["event"], ["fact"]):
        result = memory.retrieve("相机位置", user_id="user-a", case_id="case-a", scope="case",
                                 kinds=kinds, query_spec={"subject": "相机", "predicate": "位置"}, min_score=0)
        assert result["selected"] == []
        assert all(item["kind"] != "capture" for item in result["candidates"])
    assert result["timing"]["scannedCards"] == 1


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


def test_joined_entity_relation_query_keeps_exact_owner_boundary(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.sqlite3")
    saved = remember(memory, "我的包裹取件码是7392。", subject="包裹", predicate="取件码", value="7392")
    remember(memory, "同事包裹取件码是8261。", subject="同事包裹", predicate="取件码", value="8261")
    result = memory.retrieve("我的包裹取件码是多少？", user_id="user-a", case_id="case-a", scope="case",
                             query_spec={"subject": "包裹取件码", "predicate": None})
    assert [row["cardId"] for row in result["selected"]] == [saved["id"]]


def test_verbatim_memory_resolves_only_constraints_present_in_saved_text(tmp_path):
    path = tmp_path / "timeline.sqlite3"
    memory = TimelineMemory(path)
    saved = remember(memory, "记住：我的储物柜编号是7429。",
                     subject="用户显式声明", predicate="verbatim")
    restored = TimelineMemory(path)
    for subject, predicate, expected in (("储物柜", "编号", True),
                                         ("保险柜", "编号", False),
                                         ("储物柜", "位置", False)):
        result = restored.retrieve("我的储物柜编号是什么？", user_id="user-a",
                                   case_id="case-a", scope="case",
                                   query_spec={"subject": subject, "predicate": predicate})
        assert (saved["id"] in [r["cardId"] for r in result['selected']]) == expected


def test_subject_possessive_does_not_hide_user_memory_or_merge_named_owners(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    saved = remember(memory, "我的储物柜编号是7429。", subject="储物柜", predicate="编号")
    for subject, expected in (("我的储物柜", True), ("小李的储物柜", False), ("保险柜", False)):
        result = memory.retrieve("我的储物柜编号是什么？", user_id="user-a", case_id="case-a",
                                 scope="case", query_spec={"subject": subject, "predicate": "编号"})
        assert (saved['id'] in [r['cardId'] for r in result['selected']]) == expected


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
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    for index in range(6):
        remember(
            memory, f"用户在第{index}次练习后休息",
            kind="event", memory_key="routine:practice-rest", supersede=False,
            effective_at=base + timedelta(days=index), recorded_at=base + timedelta(days=index),
            timezone_name="Asia/Shanghai", utc_offset_minutes=480,
            subject="用户", predicate="practice.rest", value=str(index),
        )

    result = memory.promote_to_long(user_id="user-a", case_id="case-a", scope="case")
    long_cards = [
        item for item in memory.cards(user_id="user-a", case_id="case-a", scope="case")
        if item["tier"] == "long"
    ]

    assert result["status"] == "consolidated"
    assert len(long_cards) == 4
    assert all(item["sourceIds"] == [] for item in long_cards)  # promotion cannot invent source evidence
    assert all(item["subject"] == "用户" for item in long_cards)
    assert all(item["predicate"] == "practice.rest" for item in long_cards)
    assert all(item["timezone"] == "Asia/Shanghai" for item in long_cards)
    assert all(item["utcOffsetMinutes"] == 480 for item in long_cards)
    assert {item["text"] for item in long_cards} == {
        f"用户在第{index}次练习后休息" for index in range(4)
    }


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


def test_structured_fast_path_preserves_scope_and_case_isolation(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    participant = remember(
        memory,
        "participant location is home",
        scope="participant",
        case_id=None,
        kind="state",
        subject="vitamin",
        predicate="location",
        value="home",
    )
    case_a = remember(
        memory,
        "case A location is kitchen",
        case_id="case-a",
        kind="state",
        subject="vitamin",
        predicate="location",
        value="kitchen",
    )
    case_b = remember(
        memory,
        "case B location is desk",
        case_id="case-b",
        kind="state",
        subject="vitamin",
        predicate="location",
        value="desk",
    )

    query = {"subject": "vitamin", "predicate": "location"}
    assert memory.retrieve(
        "where is vitamin", user_id="user-a", case_id="case-a", scope="case", query_spec=query,
    )["selected"][0]["cardId"] == case_a["id"]
    assert memory.retrieve(
        "where is vitamin", user_id="user-a", case_id="case-b", scope="case", query_spec=query,
    )["selected"][0]["cardId"] == case_b["id"]
    assert memory.retrieve(
        "where is vitamin", user_id="user-a", case_id=None, scope="participant", query_spec=query,
    )["selected"][0]["cardId"] == participant["id"]


def test_unrelated_chinese_cards_do_not_match_on_one_shared_character(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    remember(memory, "用户把番茄放在案板旁边", kind="scene")

    result = memory.retrieve(
        "小李的电话号码是多少",
        user_id="user-a", case_id="case-a", scope="case",
    )

    assert result["selected"] == []


def test_default_retrieval_excludes_legacy_and_scene_even_when_text_matches(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    remember(memory, "开瑞坦放在书桌右边的架子上", kind="legacy")
    remember(memory, "开瑞坦放在书桌右边的架子上", kind="scene")
    fact = remember(
        memory, "开瑞坦放在书桌右边的架子上", kind="state",
        subject="开瑞坦", predicate="location", value="书桌右边的架子上",
    )

    result = memory.retrieve(
        "开瑞坦放在哪里", user_id="user-a", case_id="case-a", scope="case",
        query_spec={"subject": "开瑞坦", "predicate": "location"},
    )

    assert [item["cardId"] for item in result["selected"]] == [fact["id"]]
    assert {item["kind"] for item in result["candidates"]} == {"state"}
    assert result["minimumScore"] == DEFAULT_RETRIEVAL_MIN_SCORE


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
        tier="short",
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


def test_memory_draft_persists_and_rejects_unrelated_entity(tmp_path):
    path = tmp_path / "timeline.sqlite3"
    memory = TimelineMemory(path)
    draft = memory.begin_draft(
        user_id="user-a", case_id="case-a", subject="小李", predicate="phone",
        missing_fields=("value",),
    )

    restored = TimelineMemory(path)
    pending = restored.pending_draft(user_id="user-a", case_id="case-a")
    assert pending["id"] == draft["id"]
    assert restored.draft_accepts(pending, subject="小李", predicate="phone")
    assert not restored.draft_accepts(pending, subject="小陈", predicate="phone")
    assert not restored.draft_accepts(pending, subject="小李", predicate="location")


def test_real_civil_time_is_separate_from_recording_time(tmp_path):
    clock = ClockContext.current(
        "Asia/Shanghai", 480,
        now_utc=datetime(2026, 7, 29, 16, 30, tzinfo=timezone.utc),
    )
    temporal = resolve_time_semantics("我昨天吃过药了", clock, kind="event")
    assert temporal["recordedAt"].startswith("2026-07-29T16:30")
    assert temporal["effectiveAt"].startswith("2026-07-28T16:00")
    assert temporal["effectiveEnd"].startswith("2026-07-29T16:00")

    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    card = remember(
        memory, "我昨天吃过药了", kind="event",
        recorded_at=temporal["recordedAt"], effective_at=temporal["effectiveAt"],
        effective_end=temporal["effectiveEnd"], timezone_name=temporal["timezone"],
        utc_offset_minutes=clock.utc_offset_minutes, subject="用户", predicate="medication.taken",
        value="维生素D3",
    )
    assert card["recordedAt"] != card["effectiveAt"]
    assert card["dayKey"] == "2026-07-29"

    yesterday = memory.retrieve(
        "我昨天吃过药了吗", user_id="user-a", case_id="case-a", scope="case",
        clock=clock, query_spec={"subject": "用户", "predicate": "medication.taken"},
    )
    today = memory.retrieve(
        "我今天吃过药了吗", user_id="user-a", case_id="case-a", scope="case",
        clock=clock, query_spec={"subject": "用户", "predicate": "medication.taken"},
    )
    assert [item["cardId"] for item in yesterday["selected"]] == [card["id"]]
    assert today["selected"] == []


def test_memory_retention_uses_event_time_not_last_update_time(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    card = remember(
        memory,
        "old durable fact",
        kind="fact",
        happened_at=old,
        recorded_at=old,
    )

    result = memory.apply_retention(
        user_id="user-a", cutoff=datetime(2021, 1, 1, tzinfo=timezone.utc),
    )

    assert result["memoryCards"] == 1
    assert memory.cards(
        user_id="user-a", case_id="case-a", scope="case", statuses=("active", "deleted"),
    ) == []


def test_daily_recurrence_uses_participant_timezone():
    clock = ClockContext.current(
        "Asia/Shanghai", 480,
        now_utc=datetime(2026, 7, 29, 5, 0, tzinfo=timezone.utc),
    )
    temporal = resolve_time_semantics("我每天中午十二点吃两颗", clock, kind="preference")
    assert temporal["recurrence"] == {
        "frequency": "daily", "localTime": "12:00", "timezone": "Asia/Shanghai",
    }


def test_entity_qualified_key_and_retrieval_never_mix_people(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    li = remember(
        memory, "小李的电话是13659892706", scope="participant", case_id=None,
        subject="小李", predicate="phone", value="13659892706",
    )
    chen = remember(
        memory, "小陈的电话是12358", scope="participant", case_id=None,
        subject="小陈", predicate="phone", value="12358",
    )
    assert li["memoryKey"] == memory_key_for("小李", "phone") == "contact:小李:phone"
    assert chen["memoryKey"] == "contact:小陈:phone"
    assert li["status"] == chen["status"] == "active"

    result = memory.retrieve(
        "小李电话是多少", user_id="user-a", case_id=None, scope="participant",
        query_spec={"subject": "小李", "predicate": "phone"},
    )
    assert [item["cardId"] for item in result["selected"]] == [li["id"]]
    rejected = next(item for item in result["candidates"] if item["cardId"] == chen["id"])
    assert rejected["rejectionReasons"] == ["subject-mismatch"]


def test_v1_database_migrates_in_place_without_losing_cards(tmp_path):
    path = tmp_path / "timeline.sqlite3"
    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE timeline_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO timeline_meta(key, value) VALUES('schema_version', '1');
            CREATE TABLE memory_cards (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                case_id TEXT,
                scope TEXT NOT NULL,
                tier TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                memory_key TEXT,
                happened_at TEXT NOT NULL,
                day_key TEXT NOT NULL,
                embedding BLOB NOT NULL,
                embedding_dim INTEGER NOT NULL,
                source_ids TEXT NOT NULL,
                status TEXT NOT NULL,
                importance REAL NOT NULL,
                supersedes TEXT,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        connection.execute(
            """INSERT INTO memory_cards VALUES(
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            (
                "legacy-card", "user-a", "case-a", "case", "short", "fact",
                "旧版记忆", "旧版记忆", None, "2026-07-20T08:00:00+00:00", "2026-07-20",
                vector.tobytes(), 2, "[]", "active", 0.7, None, "{}",
                "2026-07-20T08:01:00+00:00", "2026-07-20T08:01:00+00:00",
            ),
        )

    restored = TimelineMemory(path)
    card = restored.cards(user_id="user-a", case_id="case-a", scope="case")[0]
    assert card["id"] == "legacy-card"
    assert card["recordedAt"] == "2026-07-20T08:01:00+00:00"
    assert card["effectiveAt"] == "2026-07-20T08:00:00+00:00"
    assert card["timezone"] == "UTC"
    assert card["utcOffsetMinutes"] == 0
    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM timeline_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert version == "3"


def test_dst_day_ranges_follow_civil_calendar_and_normalize_missing_hour():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
    except Exception:
        return

    spring = ClockContext.current(
        "America/New_York", -300,
        now_utc=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
    )
    spring_day = resolve_time_semantics("今天", spring, kind="event")
    spring_span = (
        datetime.fromisoformat(spring_day["effectiveEnd"])
        - datetime.fromisoformat(spring_day["effectiveAt"])
    )
    assert spring_span == timedelta(hours=23)

    missing = resolve_time_semantics("2026年3月8日凌晨2点30分", spring, kind="event")
    assert missing["effectiveAt"].startswith("2026-03-08T07:30")

    autumn = ClockContext.current(
        "America/New_York", -240,
        now_utc=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
    )
    autumn_day = resolve_time_semantics("今天", autumn, kind="event")
    autumn_span = (
        datetime.fromisoformat(autumn_day["effectiveEnd"])
        - datetime.fromisoformat(autumn_day["effectiveAt"])
    )
    assert autumn_span == timedelta(hours=25)


def test_clock_query_uses_exact_minute_instead_of_following_day(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    clock = ClockContext.current(
        "Asia/Shanghai", 480,
        now_utc=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
    )
    at_three = resolve_time_semantics("2026年7月29日下午3点", clock, kind="event")
    at_four = resolve_time_semantics("2026年7月29日下午4点", clock, kind="event")
    first = remember(
        memory, "用户下午三点服药", kind="event",
        effective_at=at_three["effectiveAt"], subject="用户", predicate="medication.taken",
    )
    remember(
        memory, "用户下午四点服药", kind="event",
        effective_at=at_four["effectiveAt"], subject="用户", predicate="medication.taken",
    )

    result = memory.retrieve(
        "2026年7月29日下午3点服药了吗", user_id="user-a", case_id="case-a", scope="case",
        clock=clock, query_spec={"subject": "用户", "predicate": "medication.taken"},
    )
    assert [item["cardId"] for item in result["selected"]] == [first["id"]]


def test_draft_with_known_subject_rejects_subjectless_atom(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    draft = memory.begin_draft(
        user_id="user-a", case_id="case-a", subject="小李", predicate="phone",
    )
    assert not memory.draft_accepts(draft, subject=None, predicate="phone")
