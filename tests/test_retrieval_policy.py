from egoassist.memory.retrieval_policy import EvidenceFirstPolicy

class T:
    def retrieve(self, *a, **k):
        return {"hardCandidates":[{"cardId":"a", "hardEligible":True},{"cardId":"b", "hardEligible":True}],"routing":{"temporalStatus":"not-requested"}}

class S:
    def search(self,q,limit): return [("b",.9),("a",.8)]

def test_semantic_ranking_cannot_invent_sources():
    d=EvidenceFirstPolicy(T(),S()).retrieve("q",user_id="u",case_id="c")
    assert [x["cardId"] for x in d.selected] == ["b","a"]

def test_semantic_failure_is_audited():
    class Bad:
        def search(self,*a): raise RuntimeError("down")
    d=EvidenceFirstPolicy(T(),Bad()).retrieve("q",user_id="u",case_id="c")
    assert d.rejection_reasons["semantic"].startswith("unavailable:")

def test_semantic_and_lexical_scores_are_fused():
    d=EvidenceFirstPolicy(T(),S()).retrieve("q",user_id="u",case_id="c",limit=2)
    assert {x["cardId"] for x in d.selected} == {"a","b"}
