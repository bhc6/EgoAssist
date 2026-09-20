from egoassist.memory import TimelineMemory


def test_full_candidate_audit_does_not_change_eligibility_or_selection(tmp_path):
    memory = TimelineMemory(tmp_path / "timeline.db")
    for index in range(35):
        memory.remember("Alice cup on desk " + str(index), user_id="u", case_id="c", scope="case", kind="event", card_id=str(index))
    memory.remember("She moved her cup.", user_id="u", case_id="c", scope="case", kind="event", card_id="ambiguous")
    args = dict(user_id="u", case_id="c", scope="case", include_hard_candidates=True)
    baseline = memory.retrieve("cup", **args)
    audited = memory.retrieve("cup", include_candidate_audit=True, **args)
    assert "candidateAudit" not in baseline
    assert len(audited["candidateAudit"]) == 36
    assert len(audited["candidates"]) == 20
    assert baseline["selected"] == audited["selected"]
    assert baseline["hardCandidates"] == audited["hardCandidates"]
    assert "ambiguous" not in {r["cardId"] for r in audited["hardCandidates"]}
