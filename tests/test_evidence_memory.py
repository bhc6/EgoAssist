from datetime import datetime, timezone

from egoassist.memory import ClockContext, TimelineMemory, resolve_time_semantics
from egoassist.memory.evidence import ground_context
from egoassist.realtime.episodes import RealtimeEpisodeMemory, retrieval_audit_from_events


CLOCK = ClockContext.current("Asia/Shanghai", now_utc=datetime(2026, 9, 9, 2, tzinfo=timezone.utc))


def context(discussion="昨天", agreement="前天"):
    return {"entities": ["张岚", "李宁"], "scene": "咖啡馆", "times": [
        {"relation": "讨论", "expression": discussion, "anchor": "turn"},
        {"relation": "约定", "expression": agreement, "anchor": "turn"},
    ]}


def test_multiple_event_dates_are_not_silently_collapsed():
    resolved = resolve_time_semantics("昨天讨论的前天的约定", CLOCK, kind="event")
    assert resolved["ambiguous"] is True
    assert resolved["effectiveAt"] is None
    assert resolved["recordedAt"].startswith("2026-09-09")


def test_ungrounded_time_role_requires_clarification_instead_of_false_precision(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    result = memory.retrieve("昨天讨论的前天约定", user_id="u1", case_id="a", scope="case", clock=CLOCK,
                             query_spec={"context": {"times": [
                                 {"relation": "discussion", "expression": "昨天", "anchor": "turn"},
                             ]}})
    assert result["routing"]["temporalStatus"] == "needs-clarification"
    assert result["routing"]["evidenceContext"]["times"][0]["reason"] == "not-in-source"


def test_time_anchors_use_server_civil_time_and_source_grounding():
    source = "昨天张岚讨论了相对于那天的前天与李宁的约定，地点在咖啡馆。"
    candidate = context()
    candidate["times"][1]["anchor"] = "0"
    candidate["entities"].append("没有提到的人")
    result = ground_context(candidate, source, CLOCK)
    assert result["times"][0]["start"] == "2026-09-07T16:00:00.000+00:00"
    assert result["times"][1]["start"] == "2026-09-05T16:00:00.000+00:00"
    assert result["entities"] == ["张岚", "李宁"]
    assert result["rejected"][0]["reason"] == "not-in-source"
    candidate["times"][1]["anchor"] = "ambiguous"
    assert ground_context(candidate, source, CLOCK)["times"][1]["start"] is None


def test_dual_time_recall_rejects_swapped_events_and_other_owners(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    source = "昨天张岚和李宁在咖啡馆讨论了前天的约定：由李宁提交青色样机。"
    grounded = ground_context(context(), source, CLOCK)
    target = memory.remember(source, user_id="u1", case_id="old", scope="participant", kind="event",
                             subject="张岚", predicate="讨论", metadata={"evidenceContext": grounded},
                             recorded_at=CLOCK.now_utc, source_ids=["original-turn"])
    swapped = "前天张岚和李宁在咖啡馆讨论了昨天的约定：由张岚提交红色样机。"
    memory.remember(swapped, user_id="u1", case_id="old", scope="participant", kind="event",
                    metadata={"evidenceContext": ground_context(context("前天", "昨天"), swapped, CLOCK)},
                    recorded_at=CLOCK.now_utc)
    memory.remember(source, user_id="u2", case_id="private", scope="participant", kind="event",
                    metadata={"evidenceContext": grounded}, recorded_at=CLOCK.now_utc)
    query = "张岚和李宁昨天在咖啡馆讨论的前天的约定是什么？"
    result = memory.retrieve(query, user_id="u1", case_id="new", scope="participant", clock=CLOCK,
                             query_spec={"subject": "张岚", "predicate": "约定", "context": context()})
    assert [item["cardId"] for item in result["selected"]] == [target["id"]]
    assert all(item["card"]["userId"] == "u1" for item in result["candidates"])
    assert any("time-mismatch" in item["rejectionReasons"] for item in result["candidates"])
    assert result["calibratedProbability"] is None
    reopened = TimelineMemory(memory.path)
    exported = reopened.export_user("u1")["cards"]
    assert any(card["sourceIds"] == ["original-turn"] for card in exported)
    network = reopened.snapshot(user_id="u1", case_id=None, scope="participant")["evidenceNetwork"]
    assert {node["label"] for node in network["nodes"]} == {"张岚", "李宁"}
    assert len(network["events"]) == 2


def test_recall_injection_keeps_event_times_and_provenance(tmp_path):
    memory = RealtimeEpisodeMemory(tmp_path / "runtime.json", owner_user_id="u1", scope="participant")
    source = "昨天张岚和李宁在咖啡馆讨论前天的约定。"
    card = memory.remember(source, kind="event", source="user-transcript", skill=None, task=None,
                           session_id="original", evidence_event_id="turn-1", recorded_at=CLOCK.now_utc.isoformat(),
                           evidence_context=ground_context(context(), source, CLOCK))
    retrieved = memory.retrieve("张岚的约定", session_id="new", allow_cross_session=True, clock=CLOCK)
    evidence = memory.mark_injected(retrieved)["evidence"][0]
    assert evidence["cardId"] == card["id"]
    assert evidence["sourceIds"] == ["turn-1"]
    assert evidence["recordedAt"] == card["recordedAt"]
    assert len(evidence["evidenceContext"]["times"]) == 2


def test_last_retrieval_can_be_rebuilt_without_live_memory_cache():
    events = [
        {"eventId": "retrieved", "parentEventIds": ["turn-2"], "type": "episodic.memory.retrieved",
         "payload": {"query": "约定", "candidates": [{"episodeId": "card-1", "selected": True, "score": 0.3}]}},
        {"eventId": "injected", "parentEventIds": ["turn-2"], "type": "episodic.memory.injected",
         "payload": {"status": "injected", "evidence": [{"episodeId": "card-1", "summary": "约定原文", "sourceIds": ["turn-1"]}]}},
    ]
    snapshot = retrieval_audit_from_events(events)
    assert snapshot["injection"]["evidence"][0]["sourceIds"] == ["turn-1"]
    assert snapshot["selected"][0]["record"]["summary"] == "约定原文"
    events[1]["parentEventIds"] = ["other-turn"]
    assert retrieval_audit_from_events(events) is None
