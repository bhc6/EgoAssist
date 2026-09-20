from datetime import datetime, timezone

import pytest

from egoassist.memory import TimelineMemory


def test_vector_nomination_preserves_scope_status_reference_and_time(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.db")
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    defaults = dict(user_id="u", case_id="c", scope="case", kind="event", happened_at=now)
    rows = [
        ("valid", "Alice holds a cup", {}),
        ("other-user", "Alice holds a cup", {"user_id": "other"}),
        ("other-case", "Alice holds a cup", {"case_id": "other"}),
        ("deleted", "Alice holds a cup", {"status": "deleted"}),
        ("capture", "Alice holds a cup", {"kind": "capture"}),
        ("ambiguous", "She holds her cup", {}),
        ("yesterday", "Alice holds a cup", {"happened_at": datetime(2026, 9, 13, 12, tzinfo=timezone.utc)}),
    ]
    for sid, text, overrides in rows:
        memory.remember(text, **(defaults | overrides), card_id=sid, source_ids=[sid], supersede=False)
    kwargs = dict(user_id="u", case_id="c", scope="case", now=now,
                  include_candidate_audit=True, include_hard_candidates=True)
    result = memory.retrieve("today cup", candidate_card_ids=[r[0] for r in rows] + ["missing"], **kwargs)
    assert {r["cardId"] for r in result["hardCandidates"]} == {"valid"}
    assert {r["cardId"] for r in result["candidateAudit"]} == {"valid", "ambiguous", "yesterday"}
    assert memory.retrieve("cup", candidate_card_ids=[], **kwargs)["hardCandidates"] == []
    assert memory.retrieve("cup", candidate_card_ids=["valid"], kinds=[], **kwargs)["hardCandidates"] == []


def test_vector_nomination_is_bounded_and_does_not_fill_from_other_records(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOASSIST_MEMORY_CANDIDATE_LIMIT", "32")
    memory = TimelineMemory(tmp_path / "timeline.db")
    args = dict(user_id="u", case_id="c", scope="case")
    for index in range(40):
        memory.remember("Alice cup " + str(index), **args, kind="event", card_id=str(index))
    result = memory.retrieve("cup", **args, candidate_card_ids=["0", "0"], include_hard_candidates=True)
    assert [r["cardId"] for r in result["hardCandidates"]] == ["0"]
    with pytest.raises(ValueError, match="candidate limit"):
        memory.retrieve("cup", **args, candidate_card_ids=[str(i) for i in range(33)])


def test_vector_nomination_cannot_resurrect_corrected_fact_or_override_subject(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.db")
    args = dict(user_id="u", case_id="c", scope="case")
    old = memory.remember("Alice locker number 12", **args, kind="state", subject="Alice",
                          predicate="number", value="12", card_id="old")
    new = memory.remember("Alice locker number 34", **args, kind="state", subject="Alice",
                          predicate="number", value="34", card_id="new")
    result = memory.retrieve("locker number", **args, candidate_card_ids=[old["id"], new["id"]],
                             include_hard_candidates=True)
    assert [r["cardId"] for r in result["hardCandidates"]] == [new["id"]]
    other = memory.retrieve("locker number", **args, candidate_card_ids=[new["id"]],
                            query_spec={"subject": "Bob"}, include_hard_candidates=True)
    assert other["hardCandidates"] == []


def test_semantic_evidence_without_word_overlap_still_requires_hard_gates(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.db")
    args = dict(user_id="u", case_id="c", scope="case")
    memory.remember("Alice holds a mug", **args, kind="event", card_id="a")
    result = memory.retrieve("beverage vessel", **args, candidate_card_ids=["a"], include_hard_candidates=True)
    assert [r["cardId"] for r in result["hardCandidates"]] == ["a"]
    assert result["selected"] == []
