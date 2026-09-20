"""Evidence-first memory retrieval policy.

Semantic indexes may expand and rank candidates, but they cannot create facts or
override temporal/identity constraints enforced by TimelineMemory.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol

class SemanticRetriever(Protocol):
    def search(self, query: str, limit: int) -> list[tuple[str, float]]: ...

@dataclass(frozen=True)
class RetrievalDecision:
    selected: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    status: str
    rejection_reasons: dict[str, str]
    confidence: float | None = None
    clarification: str | None = None

class EvidenceFirstPolicy:
    """Coordinate structured grounding and optional semantic ranking."""
    def __init__(self, timeline, semantic: SemanticRetriever | None = None, *, max_candidates: int = 50):
        self.timeline, self.semantic, self.max_candidates = timeline, semantic, max_candidates

    def retrieve(self, query: str, *, user_id: str, case_id: str, scope: str = "case", limit: int = 5, **kwargs) -> RetrievalDecision:
        kwargs["include_hard_candidates"] = True
        base = self.timeline.retrieve(query, user_id=user_id, case_id=case_id, scope=scope, limit=self.max_candidates, **kwargs)
        # Fail closed on older providers: their audit candidates are not evidence.
        pool = [x for x in base.get("hardCandidates", base.get("selected", []))
                if x.get("hardEligible", True)]
        if base.get("routing", {}).get("temporalStatus") == "needs-clarification":
            pool = []
        reasons: dict[str, str] = {}
        semantic_ranked = False
        if self.semantic is not None and pool:
            try:
                semantic_hits = self.semantic.search(query, self.max_candidates)
                allowed = {x.get("cardId") or x.get("episodeId") for x in pool}
                ranks = {}
                for sid, _ in semantic_hits:
                    if sid in allowed and sid not in ranks:
                        ranks[sid] = len(ranks) + 1
                lexical = {x.get("cardId") or x.get("episodeId"): i + 1 for i, x in enumerate(pool)}
                def fused(item):
                    sid = item.get("cardId") or item.get("episodeId")
                    lrank = lexical.get(sid, self.max_candidates + 1)
                    return 1.0 / (60 + lrank) + (1.0 / (60 + ranks[sid]) if sid in ranks else 0.0)
                pool.sort(key=lambda x: (-fused(x), ranks.get(x.get("cardId"), self.max_candidates + 1)))
                semantic_ranked = bool(ranks)
            except Exception as exc:
                reasons["semantic"] = f"unavailable:{type(exc).__name__}"
        allowed = {x.get("cardId") or x.get("episodeId") for x in pool}
        selected = (pool if semantic_ranked else [x for x in base.get("selected", [])
                    if (x.get("cardId") or x.get("episodeId")) in allowed])[:max(0, limit)]
        status = base.get("routing", {}).get("temporalStatus", "unknown")
        if not selected and status not in {"needs-clarification", "unknown"}: status = "no-evidence"
        # Rank gaps are not calibrated truth probabilities. Selective answering
        # is evaluated separately before a threshold can be promoted here.
        return RetrievalDecision(selected, pool, status, reasons)
