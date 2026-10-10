from egoassist.memory.retrieval_policy import EvidenceFirstPolicy

class T:
    def retrieve(self, *a, **k):
        return {"hardCandidates":[{"cardId":"a", "hardEligible":True},{"cardId":"b", "hardEligible":True}],"routing":{"temporalStatus":"not-requested"}}

class S:
    # "zzz" is NOT a hard candidate: a semantic retriever nominates, it can
    # never add evidence the eligibility layer did not admit.
    def search(self,q,limit): return [("zzz",.99),("b",.9),("a",.8)]

def test_semantic_ranking_cannot_invent_sources():
    d=EvidenceFirstPolicy(T(),S()).retrieve("q",user_id="u",case_id="c")
    assert [x["cardId"] for x in d.selected] == ["b","a"]

def test_semantic_failure_is_audited():
    class Bad:
        def search(self,*a): raise RuntimeError("down")
    d=EvidenceFirstPolicy(T(),Bad()).retrieve("q",user_id="u",case_id="c")
    assert d.rejection_reasons["semantic"].startswith("unavailable:")
