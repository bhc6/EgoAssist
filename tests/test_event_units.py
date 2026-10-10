from datetime import datetime, timezone

import pytest

from egoassist.memory import TimelineMemory, build_event_units, remember_event_unit
from egoassist.memory.event_units import split_speaker

ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)


def rec(sid, start, end, text, kind="caption", day="DAY2"):
    return {"sourceId": sid, "start": start, "end": end, "text": text, "kind": kind, "day": day}


def test_split_speaker_parses_label_and_keeps_unlabelled_text():
    assert split_speaker("Alice: 我们走吧") == ("Alice", "我们走吧")
    assert split_speaker("小李：好的") == ("小李", "好的")
    assert split_speaker("我打开电脑") == (None, "我打开电脑")


def test_units_merge_close_cues_and_split_on_gap_span_and_group():
    records = [
        rec("c1", 0, 2, "我打开电脑"), rec("t1", 1, 3, "Alice: 帮我看下数据", "transcript"),
        rec("c2", 4, 6, "查看电脑数据"),
        rec("c3", 20, 22, "我走向厨房"),              # gap 14 s > 8 s
        rec("c4", 23, 60, "我在切菜"),                 # would exceed 30 s span
        rec("c5", 61, 62, "第二天的画面", day="DAY3"),  # different group
    ]
    units = build_event_units(records, group_key=lambda r: r["day"])
    assert [u.source_ids for u in units] == [["c1", "t1", "c2"], ["c3"], ["c4"], ["c5"]]
    first = units[0]
    assert (first.start, first.end) == (0, 6)
    assert first.speakers == ["Alice"]
    assert "画面：我打开电脑；查看电脑数据" in first.text()
    assert "Alice：帮我看下数据" in first.text()


def test_units_reject_inverted_interval_and_bad_bounds():
    with pytest.raises(ValueError):
        build_event_units([rec("x", 5, 4, "bad")])
    with pytest.raises(ValueError):
        build_event_units([], max_span=0)


def test_event_card_keeps_interval_sources_wearer_and_is_recallable_under_strict(tmp_path, monkeypatch):
    monkeypatch.delenv("EGOASSIST_MEMORY_REFERENCE_POLICY", raising=False)
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    unit = build_event_units([
        rec("c1", 100, 102, "我把这个杯子放到桌上"), rec("t1", 101, 104, "Alice: 他明天来拿", "transcript"),
    ])[0]
    card = remember_event_unit(memory, unit, user_id="u", case_id="c", origin=ORIGIN,
                               namespace="test", wearer="Jake", time_basis="synthetic-relative-day")
    assert card["effectiveAt"].startswith("2000-01-01T00:01:40")
    assert card["effectiveEnd"].startswith("2000-01-01T00:01:44")
    # "明天" inside speech must not move a measured interval.
    assert card["grounding"]["temporalStatus"] == "resolved"
    assert card["sourceIds"] == ["c1", "t1"]
    assert card["metadata"]["speakers"] == ["Alice"]
    # Deictics are present, but the wearer is a source-grounded subject.
    assert card["grounding"]["referenceStatus"] == "source-scoped"
    result = memory.retrieve("杯子放到哪了", user_id="u", case_id="c", scope="case", min_score=0)
    assert [item["cardId"] for item in result["selected"]] == [card["id"]]


def test_event_card_write_is_idempotent(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    unit = build_event_units([rec("c1", 0, 2, "我打开电脑")])[0]
    first = remember_event_unit(memory, unit, user_id="u", case_id="c", origin=ORIGIN, namespace="n", wearer="Jake")
    again = remember_event_unit(memory, unit, user_id="u", case_id="c", origin=ORIGIN, namespace="n", wearer="Jake")
    assert first["id"] == again["id"]
    assert len(memory.cards(user_id="u", case_id="c", scope="case")) == 1


def test_another_speakers_first_person_is_not_attributed_to_the_wearer(tmp_path, monkeypatch):
    monkeypatch.delenv("EGOASSIST_MEMORY_REFERENCE_POLICY", raising=False)
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    unit = build_event_units([
        rec("c1", 0, 2, "画面里有一串钥匙"), rec("t1", 1, 3, "Alice: 我拿走了钥匙", "transcript"),
    ])[0]
    card = remember_event_unit(memory, unit, user_id="u", case_id="c", origin=ORIGIN,
                               namespace="n", wearer="Jake")
    speaker = card["metadata"]["evidenceContext"]["speaker"]
    assert speaker["wearer"] is False and speaker["labels"] == ["Alice"]
    # "我" here is Alice, not Jake, so it is not a source-scoped claim about Jake.
    assert card["grounding"]["referenceStatus"] == "needs-clarification"


def test_units_from_different_groups_with_overlapping_clocks_stay_whole():
    records = [rec("a1", 0, 2, "甲一", day="DAY2"), rec("b1", 1, 3, "乙一", day="DAY3"),
               rec("a2", 3, 5, "甲二", day="DAY2"), rec("b2", 4, 6, "乙二", day="DAY3")]
    units = build_event_units(records, group_key=lambda r: r["day"])
    assert [u.source_ids for u in units] == [["a1", "a2"], ["b1", "b2"]]
