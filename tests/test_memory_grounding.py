from egoassist.realtime.semantic import SemanticTurnRouter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import asyncio

import pytest

from egoassist.memory import ClockContext, TimelineMemory, resolve_time_semantics
from egoassist.memory.evidence import ground_context
from egoassist.realtime.context_harness import ContextHarness, SessionJournal
from egoassist.realtime.episodes import RealtimeEpisodeMemory
from egoassist.realtime.work_ledger import CaseWorkLedger
from egoassist.memory.recovery import MemoryRecovery, recovery_jobs


SOURCE = ClockContext.current("Asia/Shanghai", now_utc=datetime(2026, 9, 8, 2, tzinfo=timezone.utc))
TODAY = ClockContext.current("Asia/Shanghai", now_utc=datetime(2026, 9, 11, 2, tzinfo=timezone.utc))


def save(memory, text, **kwargs):
    return memory.remember(text, **({"user_id": "u", "case_id": "c", "scope": "case",
        "kind": "event", "recorded_at": SOURCE.now_utc, "timezone_name": "Asia/Shanghai"} | kwargs))


def recall(memory, text, **kwargs):
    return memory.retrieve(text, user_id="u", case_id="c", scope="case", clock=TODAY, **kwargs)


@pytest.mark.parametrize("kind", ["event", "fact", "state"])
@pytest.mark.parametrize("query", ["今天我吃过药了吗？", "今早我吃过药了吗？", "今日吃药了吗？"])
def test_old_relative_statement_cannot_answer_today_even_with_key(tmp_path, kind, query):
    memory = TimelineMemory(tmp_path / "memory.db")
    card = save(memory, "今天我已经吃过药了", kind=kind, subject="我", predicate="服药", value="已服")
    assert card["effectiveAt"].startswith("2026-09-07T16:00")
    restored = TimelineMemory(memory.path)
    assert recall(restored, query, query_spec={"subject": "我", "predicate": "服药"})["selected"] == []
    historic = recall(restored, "2026年9月8日我吃过药了吗？", query_spec={"subject": "我", "predicate": "服药"})
    assert [x["cardId"] for x in historic["selected"]] == [card["id"]]


def test_paraphrase_cannot_strip_source_date(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    original = "昨天我已经吃过药了"
    context = ground_context(None, original, SOURCE)
    context["sourceText"] = original
    card = save(memory, "用户已服药", kind="fact", metadata={"evidenceContext": context})
    assert card["effectiveAt"].startswith("2026-09-06T16:00")
    assert recall(memory, "今天服药了吗？")["selected"] == []


@pytest.mark.parametrize("kind", ["event", "fact", "state"])
def test_same_text_on_different_days_is_not_deduplicated_or_superseded(tmp_path, kind):
    memory = TimelineMemory(tmp_path / "memory.db")
    first = save(memory, "今天我已经吃过药了", kind=kind, source_ids=["a"], subject="我", predicate="服药")
    second = save(memory, "今天我已经吃过药了", kind=kind, source_ids=["b"], subject="我", predicate="服药", recorded_at=TODAY.now_utc)
    assert first["id"] != second["id"]
    assert len(memory.cards(user_id="u", case_id="c", scope="case")) == 2
    assert [x["cardId"] for x in recall(memory, "今天吃过药了吗？")["selected"]] == [second["id"]]


def test_retry_writes_are_atomic_across_connections_and_keep_occurrences(tmp_path):
    path = tmp_path / "memory.db"
    writers = [TimelineMemory(path), TimelineMemory(path)]
    def write(i):
        return save(writers[i % 2], "今天我已经吃过药了", source_ids=["turn-1"])["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(write, range(12)))
    assert len(set(ids)) == 1
    second = save(writers[0], "今天我已经吃过药了", source_ids=["turn-2"])
    assert second["id"] != ids[0]


def test_dedup_preserves_sources_and_distinct_subjects(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    first = save(memory, "李的编号是42", kind="fact", source_ids=["a"])
    repeat = save(memory, "李的编号是42", kind="fact", source_ids=["b"])
    assert first["id"] == repeat["id"]
    assert repeat["sourceIds"] == ["a", "b"]
    one = save(memory, "位置是门口", kind="state", subject="钥匙", predicate="位置", value="门口", source_ids=["c"])
    two = save(memory, "位置是门口", kind="state", subject="雨伞", predicate="位置", value="门口", source_ids=["d"])
    assert one["id"] != two["id"]


@pytest.mark.parametrize("text", ["2026年2月30日吃过药", "前几天吃过药", "那天吃过药", "昨天说今天吃药"])
def test_unknown_or_multiple_dates_never_fall_back_to_recording_time(tmp_path, text):
    memory = TimelineMemory(tmp_path / "memory.db")
    card = save(memory, text, recorded_at=TODAY.now_utc, effective_at=TODAY.now_utc)
    assert card["effectiveAt"] is None
    assert recall(memory, "今天吃药了吗？")["selected"] == []


def test_unresolved_query_times_and_missing_candidate_times_fail_closed(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    save(memory, "吃药", kind="fact")
    for anchor in ("turn", "ambiguous"):
        result = recall(memory, "今天吃药了吗？", query_spec={"context": {"times": [
            {"expression": "今天", "relation": "吃药", "anchor": anchor}]}})
        assert result["selected"] == []
    assert recall(memory, "那天吃药了吗？")["routing"]["temporalStatus"] == "needs-clarification"


def test_recurring_plan_does_not_prove_todays_completion(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    card = save(memory, "每天上午8点吃药", recorded_at=TODAY.now_utc)
    assert card["recurrence"]["frequency"] == "daily"
    assert card["effectiveAt"] is None
    assert recall(memory, "今天吃药了吗？")["selected"] == []


@pytest.mark.parametrize("subject,value", [("他", "42"), ("钥匙", "那里"), ("那个", "门口")])
def test_unresolved_reference_does_not_become_current_fact(tmp_path, subject, value):
    memory = TimelineMemory(tmp_path / "memory.db")
    card = save(memory, f"{subject}的位置是{value}", kind="state", subject=subject, predicate="位置", value=value)
    assert card["status"] == "pending"
    assert card["memoryKey"] is None
    assert recall(memory, "位置", query_spec={"subject": subject, "predicate": "位置"})["selected"] == []


def test_legacy_read_projection_repairs_dates_without_deleting_original(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    card = save(memory, "今天我已经吃过药了", source_ids=["old"])
    with sqlite3.connect(memory.path) as connection:
        connection.execute("UPDATE memory_cards SET effective_at=?, effective_end=NULL, metadata='{}' WHERE id=?",
            (TODAY.now_utc.isoformat(), card["id"]))
    restored = TimelineMemory(memory.path)
    assert recall(restored, "今天吃药了吗？")["selected"] == []
    evidence = restored.export_user("u")["cards"][0]
    assert evidence["text"] == "今天我已经吃过药了"
    assert evidence["grounding"]["statementLocalTime"].startswith("2026-09-08")


def test_recall_injection_exposes_absolute_source_anchor(tmp_path):
    memory = RealtimeEpisodeMemory(tmp_path / "runtime.json", owner_user_id="u", scope="participant")
    memory.remember("今天我已经吃过药了", kind="event", source="semantic-memory-card", skill=None, task=None,
        session_id="c", recorded_at=SOURCE.now_utc.isoformat(), timezone_name="Asia/Shanghai", evidence_event_id="s")
    result = memory.retrieve("吃过药的历史记录", session_id="other", allow_cross_session=True, clock=TODAY)
    evidence = memory.mark_injected(result)["evidence"][0]
    assert evidence["grounding"]["timeExpressions"][0]["localDate"] == "2026-09-08"
    assert "never the recall date" in evidence["grounding"]["claimBoundary"]


def test_history_replay_and_summary_keep_source_timestamp(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="c")
    entry = journal.append_message("user", "今天我已经吃过药了", source="test")
    harness = ContextHarness(journal)
    replay = harness.provider_replay_items()[0]["content"][0]["text"]
    assert entry["timestamp"] in replay
    assert "今天我已经吃过药了" in replay
    summary = harness._deterministic_summary(harness._context_messages()[0], None)
    assert entry["timestamp"] in summary


@pytest.mark.parametrize("phrase,date", [("今早", "2026-09-11"), ("昨晚", "2026-09-10"),
    ("三天前", "2026-09-08"), ("2 days ago", "2026-09-09"), ("tomorrow", "2026-09-12")])
def test_relative_day_expressions_use_source_civil_calendar(phrase, date):
    result = resolve_time_semantics(phrase, TODAY, kind="event")
    assert result["expressions"][0]["localDate"] == date


def test_date_aliases_and_daylight_savings_are_not_ambiguous():
    result = resolve_time_semantics("今天（2026年9月11日）", TODAY, kind="event")
    assert result["ambiguous"] is False
    dst = ClockContext.current("America/New_York", now_utc=datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    result = resolve_time_semantics("今天", dst, kind="event")
    assert datetime.fromisoformat(result["effectiveEnd"]) - datetime.fromisoformat(result["effectiveAt"]) == timedelta(hours=25)


def test_restart_keeps_unknown_event_time_unknown(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    card = save(memory, "编号是42", kind="fact", recorded_at=TODAY.now_utc)
    assert card["effectiveAt"] is None
    for _ in range(2):
        memory = TimelineMemory(memory.path)
        restored = memory.cards(user_id="u")[0]
        assert restored["effectiveAt"] is None


def test_promotion_keeps_one_occurrence_and_retry_keeps_same_id(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db", short_term_capacity=4)
    originals = [save(memory, f"事件{i}", source_ids=[f"source{i}"], recorded_at=SOURCE.now_utc + timedelta(minutes=i)) for i in range(6)]
    promotion = memory.promote_to_long(user_id="u", case_id="c", scope="case")
    cards = memory.export_user("u")["cards"]
    assert len(cards) == 6
    assert set(promotion["created"]) <= {card["id"] for card in originals}
    repeated = save(memory, "事件0", source_ids=["source0"])
    assert repeated["id"] == originals[0]["id"]
    assert repeated["tier"] == "long"
    assert len(memory.export_user("u")["cards"]) == 6


def test_correction_retires_raw_projection_and_retries_do_not_resurrect_it(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    text = "今天我已经吃过药了"
    context = ground_context(None, text, SOURCE)
    context["sourceText"] = text
    raw = save(memory, text, source_ids=["original"])
    claim = save(memory, "用户已服药", kind="fact", subject="用户", predicate="服药", value="已服",
        source_ids=["original"], metadata={"evidenceContext": context})
    correction = save(memory, "今天其实没有吃药", replaces_card_id=claim["id"], source_ids=["correction"])
    assert [card["id"] for card in memory.cards(user_id="u")] == [correction["id"]]
    retry = save(memory, text, source_ids=["original"])
    assert retry["id"] == raw["id"] and retry["status"] == "superseded"
    assert [card["id"] for card in memory.cards(user_id="u")] == [correction["id"]]


def test_missing_roles_do_not_match_a_different_event_time(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    source = "今天吃药，明天开会"
    context = ground_context({"times": [
        {"relation": "吃药", "expression": "今天", "anchor": "turn"},
        {"relation": "开会", "expression": "明天", "anchor": "turn"},
    ]}, source, TODAY)
    save(memory, source, recorded_at=TODAY.now_utc, metadata={"evidenceContext": context})
    result = recall(memory, "今天开会吗？", query_spec={"context": {"times": [
        {"relation": "开会", "expression": "今天", "anchor": "turn"}]}})
    assert result["selected"] == []


def test_delayed_message_projection_keeps_inbox_timestamp(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="c")
    journal.append("turn_inbox", {"turnId": "turn", "text": "今天吃过药了", "timestamp": SOURCE.now_utc.isoformat()})
    ledger = CaseWorkLedger(journal)
    message = ledger.ensure_turn_message("turn", "今天吃过药了", source="test")
    clock = TODAY.at_source(message["metadata"]["sourceRecordedAt"])
    assert clock.local_now.date() == SOURCE.local_now.date()
    replay = ContextHarness(journal).provider_replay_items()[0]["content"][0]["text"]
    assert SOURCE.now_utc.isoformat() in replay


def test_recovery_deduplicates_live_write_using_original_source_not_array_index(tmp_path):
    async def scenario():
        memory = TimelineMemory(tmp_path / "memory.db")
        journal = SessionJournal(tmp_path / "c.jsonl", session_id="c")
        text = "今天我已经吃过药了"
        original = save(memory, text, source_ids=["source"])
        policy = lambda *args: {"components": {"timeline": {"episodic_enabled": True, "preference_enabled": True}}}
        worker = MemoryRecovery(SemanticTurnRouter(""), memory, policy)
        worker.submit(journal, user_id="u", turn_id="turn", text=text, source_id="source", clock=SOURCE.prompt_payload())
        async def classify(*args):
            atom = {"kind": "event", "text": text, "scope": "case", "consent": "observed"}
            return {"status": "classified", "memoryAtoms": [atom, atom], "componentErrors": []}
        worker.router.classify = classify
        await worker.execute(journal, recovery_jobs(journal)["turn"])
        assert recovery_jobs(journal)["turn"]["cardIds"] == [original["id"]]
        assert len(memory.export_user("u")["cards"]) == 1
        assert recall(memory, "今天吃过药了吗？")["selected"] == []
        await worker.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["25:99吃药", "2026-02-30吃药", "上周吃药", "last week I took medicine"])
def test_unsupported_or_invalid_time_is_not_fabricated(text):
    result = resolve_time_semantics(text, SOURCE, kind="event")
    assert result["status"] == "needs-clarification"
    assert result["effectiveAt"] is None


def test_late_mutable_state_does_not_replace_newer_state(tmp_path):
    memory = TimelineMemory(tmp_path / "memory.db")
    current = save(memory, "钥匙在书桌", kind="state", subject="钥匙", predicate="位置", value="书桌", recorded_at=TODAY.now_utc)
    late = save(memory, "钥匙在门口", kind="state", subject="钥匙", predicate="位置", value="门口")
    assert late["status"] == "superseded"
    assert [card["id"] for card in memory.cards(user_id="u")] == [current["id"]]


def test_realtime_structured_keys_update_state_without_legacy_memory_key(tmp_path):
    memory = RealtimeEpisodeMemory(tmp_path / "runtime.json", owner_user_id="u", scope="participant")
    common = {"kind": "state", "source": "semantic-memory-card", "skill": None, "task": None,
        "session_id": "c", "subject": "钥匙", "predicate": "位置"}
    old = memory.remember("钥匙在门口", **common, value="门口", recorded_at=SOURCE.now_utc.isoformat(), evidence_event_id="s1")
    current = memory.remember("钥匙在书桌", **common, value="书桌", recorded_at=TODAY.now_utc.isoformat(), evidence_event_id="s2")
    result = memory.retrieve("钥匙在哪里", session_id="c", allow_cross_session=True,
        query_spec={"subject": "钥匙", "predicate": "位置"}, clock=TODAY)
    assert [row["episodeId"] for row in result["selected"]] == [current["id"]]
    assert current["id"] != old["id"]
