from datetime import datetime, timedelta, timezone

from egoassist.memory import TimelineMemory
from egoassist.memory.long_events import project_to_timeline

T0 = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)


def event(memory, text, minutes, sid):
    at = T0 + timedelta(minutes=minutes)
    return memory.remember(text, user_id="u", case_id="c", scope="case", kind="event", tier="long",
                           happened_at=at, effective_end=at + timedelta(minutes=1), source_ids=[sid],
                           metadata={"timeBasis": "measured"})


def test_digest_aggregates_contiguous_long_cards_and_is_idempotent(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    a = event(memory, "我在厨房洗菜", 0, "s1")
    b = event(memory, "我在厨房切菜", 5, "s2")
    c = event(memory, "我下楼取快递", 300, "s3")  # 5 h later: separate, single-child group
    result = memory.build_digests(user_id="u", case_id="c", scope="case")
    assert result["status"] == "digested" and len(result["created"]) == 1
    digest = memory.cards_by_ids(result["created"], user_id="u")[result["created"][0]]
    assert digest["metadata"]["childCardIds"] == [a["id"], b["id"]]
    assert digest["sourceIds"] == ["s1", "s2"]
    assert digest["effectiveAt"] == a["effectiveAt"]
    assert digest["effectiveEnd"] == b["effectiveEnd"]
    assert "洗菜" in digest["text"] and "切菜" in digest["text"] and "快递" not in digest["text"]
    # Children are kept; re-running never digests a child twice.
    assert memory.build_digests(user_id="u", case_id="c", scope="case")["created"] == []
    active = {card["id"] for card in memory.cards(user_id="u", case_id="c", scope="case")}
    assert {a["id"], b["id"], c["id"], digest["id"]} <= active


def test_forgetting_a_child_source_cascades_to_digest(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    event(memory, "我在厨房洗菜", 0, "s1")
    event(memory, "我在厨房切菜", 5, "s2")
    digest_id = memory.build_digests(user_id="u", case_id="c", scope="case")["created"][0]
    memory.forget_sources(user_id="u", source_ids=["s1"])
    assert digest_id not in {card["id"] for card in memory.cards(user_id="u", case_id="c", scope="case")}


def long_event(revision, summary, last):
    return {"eventId": "event-abc", "revision": revision, "title": "做晚饭", "summary": summary,
            "status": "ongoing", "sourceIds": ["j1", "j2"], "uncertainties": [],
            "phases": [{"description": "洗菜"}, {"description": "切菜"}],
            "coverage": {"firstEvidenceAt": T0.isoformat(), "lastEvidenceAt": last.isoformat()},
            "updatedAt": last.isoformat()}


def test_long_events_become_retrievable_cards_and_revisions_replace(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    first = project_to_timeline(memory, [long_event(1, "用户开始准备晚饭", T0 + timedelta(minutes=10))],
                                user_id="u", case_id="c")[0]
    assert first["tier"] == "long" and first["kind"] == "event"
    assert first["metadata"]["claim"] == "derived-hypothesis-not-independent-evidence"
    second = project_to_timeline(memory, [long_event(2, "用户正在炒菜做晚饭", T0 + timedelta(minutes=30))],
                                 user_id="u", case_id="c")[0]
    active = [card["id"] for card in memory.cards(user_id="u", case_id="c", scope="case")]
    assert active == [second["id"]]
    result = memory.retrieve("晚饭做到哪一步了", user_id="u", case_id="c", scope="case", min_score=0)
    assert [item["cardId"] for item in result["selected"]] == [second["id"]]


def test_deleting_a_child_retires_the_digest_that_quotes_it(tmp_path):
    """A digest copies its children's text; deleting a child must not leave
    that text recallable through the digest."""
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    secret = event(memory, "我把备用钥匙藏在花盆下面", 0, "s1")
    event(memory, "我在厨房切菜", 5, "s2")
    digest_id = memory.build_digests(user_id="u", case_id="c", scope="case")["created"][0]
    assert "花盆" in memory.cards_by_ids([digest_id], user_id="u")[digest_id]["text"]

    memory.delete(secret["id"])

    active = memory.cards(user_id="u", case_id="c", scope="case")
    assert digest_id not in {card["id"] for card in active}
    assert all("花盆" not in card["text"] for card in active)
    recalled = memory.retrieve("备用钥匙藏在哪", user_id="u", case_id="c", scope="case", min_score=0)
    assert all("花盆" not in item["card"]["text"] for item in recalled["selected"])


def test_long_event_revision_replaces_newest_active_even_when_one_is_missing(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    first = project_to_timeline(memory, [long_event(1, "用户开始准备晚饭", T0 + timedelta(minutes=10))],
                                user_id="u", case_id="c")[0]
    # Revision 2 was never written (e.g. its projection failed); 3 arrives.
    third = project_to_timeline(memory, [long_event(3, "用户吃完晚饭在洗碗", T0 + timedelta(minutes=50))],
                                user_id="u", case_id="c")[0]
    active = [card["id"] for card in memory.cards(user_id="u", case_id="c", scope="case")]
    assert active == [third["id"]] and first["id"] not in active
