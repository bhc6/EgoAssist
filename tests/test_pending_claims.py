"""Pending claims (inferred-consent fact/event/state) can now be confirmed or rejected.

Previously only preference candidates had an activation path, so every other
pending card stayed unrecallable forever.
"""

import pytest

from egoassist.memory import TimelineMemory


def remember(memory, text, **overrides):
    values = {"user_id": "u", "case_id": "c", "scope": "case", "kind": "event", "status": "pending"}
    values.update(overrides)
    return memory.remember(text, **values)


def recall(memory, query):
    return [item["cardId"] for item in memory.retrieve(query, user_id="u", case_id="c", scope="case")["selected"]]


def test_pending_claim_is_listed_and_not_recallable_until_confirmed(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    card = remember(memory, "用户把护照放进了蓝色背包")
    assert [item["id"] for item in memory.pending_claims(user_id="u", case_id="c", scope="case")] == [card["id"]]
    assert recall(memory, "护照放在哪") == []

    confirmed = memory.resolve_pending(card["id"], user_id="u", accepted=True)
    assert confirmed["status"] == "active"
    assert recall(memory, "护照放在哪") == [card["id"]]
    assert memory.pending_claims(user_id="u", case_id="c", scope="case") == []


def test_rejected_claim_is_soft_deleted(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    card = remember(memory, "用户把护照放进了蓝色背包")
    assert memory.resolve_pending(card["id"], user_id="u", accepted=False)["status"] == "deleted"
    assert recall(memory, "护照放在哪") == []


def test_confirmed_state_claim_supersedes_prior_value(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    old = memory.remember("护照在抽屉里", user_id="u", case_id="c", scope="case", kind="state",
                          subject="护照", predicate="location", value="抽屉")
    new = remember(memory, "护照在背包里", kind="state", subject="护照", predicate="location", value="背包")
    memory.resolve_pending(new["id"], user_id="u", accepted=True)
    active = {card["id"] for card in memory.cards(user_id="u", case_id="c", scope="case")}
    assert active == {new["id"]} and old["id"] not in active


def test_resolve_refuses_other_owner_wrong_kind_and_non_pending(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    card = remember(memory, "用户把护照放进了蓝色背包")
    with pytest.raises(ValueError, match="not found"):
        memory.resolve_pending(card["id"], user_id="someone-else", accepted=True)
    with pytest.raises(ValueError, match="not found"):
        memory.resolve_pending("missing", user_id="u", accepted=True)

    pref = remember(memory, "做菜时先提醒我洗手", kind="preference")
    with pytest.raises(ValueError, match="not a pending claim"):
        memory.resolve_pending(pref["id"], user_id="u", accepted=True)

    memory.resolve_pending(card["id"], user_id="u", accepted=True)
    with pytest.raises(ValueError, match="not pending"):
        memory.resolve_pending(card["id"], user_id="u", accepted=True)
