"""EGOASSIST_MEMORY_REFERENCE_POLICY: strict (default) / demote / off.

An unresolved reference is a card whose source contains a pronoun or deictic
but no extracted subject, e.g. "他把钥匙放在桌上".
"""

import pytest

from egoassist.memory import TimelineMemory


DEICTIC = "他把钥匙放在桌上"
SCOPED = "钥匙放在桌上"
QUERY = "钥匙放在哪"


def remember(memory, text, **overrides):
    values = {"user_id": "u", "case_id": "c", "scope": "case", "kind": "fact"}
    values.update(overrides)
    return memory.remember(text, **values)


def recall(memory, query=QUERY):
    return memory.retrieve(query, user_id="u", case_id="c", scope="case")


def ids(result):
    return [item["cardId"] for item in result["selected"]]


def test_default_policy_is_strict(tmp_path, monkeypatch):
    monkeypatch.delenv("EGOASSIST_MEMORY_REFERENCE_POLICY", raising=False)
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    card = remember(memory, DEICTIC)
    assert card["grounding"]["referenceStatus"] == "needs-clarification"
    assert card["status"] == "pending"
    result = recall(memory)
    assert ids(result) == []
    assert result["routing"]["referencePolicy"] == "strict"


@pytest.mark.parametrize("policy", ["demote", "off"])
def test_relaxed_policy_keeps_unresolved_card_active_and_recallable(tmp_path, monkeypatch, policy):
    monkeypatch.setenv("EGOASSIST_MEMORY_REFERENCE_POLICY", policy)
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    card = remember(memory, DEICTIC)
    # The grounding label is still truthful; only its consequence changed.
    assert card["grounding"]["referenceStatus"] == "needs-clarification"
    assert card["status"] == "active"
    result = recall(memory)
    assert ids(result) == [card["id"]]
    assert result["routing"]["referencePolicy"] == policy


@pytest.mark.parametrize("policy", ["demote", "off"])
def test_unresolved_reference_never_owns_a_canonical_key(tmp_path, monkeypatch, policy):
    # A pronoun subject must not produce "location:它" and overwrite
    # an unrelated card that happens to share the same pronoun.
    monkeypatch.setenv("EGOASSIST_MEMORY_REFERENCE_POLICY", policy)
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    first = remember(memory, "它在抽屉里", kind="state", subject="它", predicate="location", value="抽屉")
    second = remember(memory, "它在门口", kind="state", subject="它", predicate="location", value="门口")
    assert first["memoryKey"] is None and second["memoryKey"] is None
    active = memory.cards(user_id="u", case_id="c", scope="case")
    assert {card["id"] for card in active} == {first["id"], second["id"]}


def test_demote_ranks_unresolved_reference_below_source_scoped_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOASSIST_MEMORY_REFERENCE_POLICY", "demote")
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    deictic = remember(memory, DEICTIC)
    scoped = remember(memory, SCOPED)
    assert scoped["grounding"]["referenceStatus"] == "source-scoped"
    result = recall(memory)
    assert ids(result)[:2] == [scoped["id"], deictic["id"]]
    scores = {item["cardId"]: item["score"] for item in result["selected"]}
    assert 0 < scores[deictic["id"]] < scores[scoped["id"]]


def test_invalid_policy_falls_back_to_strict(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOASSIST_MEMORY_REFERENCE_POLICY", "loose")
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    assert remember(memory, DEICTIC)["status"] == "pending"
    assert recall(memory)["routing"]["referencePolicy"] == "strict"
