from datetime import datetime, timezone
import json

import numpy as np
import pytest

from egoassist.eval.controlled_retrieval import (
    ARMS, BM25, ControlledRetriever, FROZEN_CLOCK, Provider, apply_selection,
    calibrate, experiment_splits, fragments, metrics, rrf,
)
from egoassist.eval.egolife_experiments import EgoLifeSample, RetrievalQuery, evaluate_adapter, ranking_metrics
from egoassist.memory.retrieval_policy import EvidenceFirstPolicy


class Embeddings:
    def encode(self, texts, role):
        # Every item ties: even an infinitely strong semantic match cannot admit
        # excluded sources. Ranking quality is deliberately irrelevant here.
        return np.asarray([[1., 0.] for _ in texts])


@pytest.mark.parametrize("arm", ARMS)
def test_every_arm_preserves_time_identity_correction_and_source_boundaries(tmp_path, arm):
    adapter = ControlledRetriever(tmp_path / f"{arm}.db", arm, provider=Embeddings())
    records = [{"sourceId": sid, "videoId": "v", "text": text} for sid, text in (
        ("safe", "Alice blue cup on desk"), ("deleted", "Alice blue cup deleted"),
        ("superseded", "Alice blue cup old"), ("pending", "Alice blue cup pending"),
        ("wrong-user", "Alice blue cup private"), ("wrong-case", "Alice blue cup other case"),
        ("bob", "Bob blue cup on shelf"), ("yesterday", "Alice blue cup yesterday"),
    )]
    adapter.ingest(records)
    for sid, status in (("deleted", "deleted"), ("superseded", "superseded"), ("pending", "pending")):
        adapter.memory.update_status(sid, status)
    # Mutate fixtures at storage level to isolate the retrieval boundary from
    # ingestion logic. Existing Timeline tests exercise actual correction writes.
    with adapter.memory._connect() as db:
        db.execute("UPDATE memory_cards SET user_id='other' WHERE id='wrong-user'")
        db.execute("UPDATE memory_cards SET case_id='other' WHERE id='wrong-case'")
    result = adapter.retrieve(RetrievalQuery("blue cup"), query_spec={"subject": "Alice"})
    assert set(result["candidateIds"]) <= {"safe", "yesterday"}
    assert set(result["selectedIds"]) <= set(result["candidateIds"])
    assert "safe" in result["candidateIds"]
    dated = adapter.retrieve(RetrievalQuery("今天 blue cup"), query_spec={"subject": "Alice"})
    assert "yesterday" not in dated["candidateIds"]
    unresolved = adapter.retrieve(RetrievalQuery("那天 blue cup"))
    assert unresolved["candidateIds"] == unresolved["selectedIds"] == []
    # Sources injected outside the benchmark ledger cannot become citations.
    adapter.memory.remember("Alice blue cup", user_id="egolife", case_id="dataset", scope="case",
                            kind="event", card_id="not-in-ledger", recorded_at=FROZEN_CLOCK)
    assert "not-in-ledger" not in adapter.retrieve(RetrievalQuery("blue cup"))["selectedIds"]


@pytest.mark.parametrize("arm", ARMS)
def test_full_hard_pool_exceeds_audit_limit_and_zero_overlap_is_not_hard_rejection(tmp_path, arm):
    adapter = ControlledRetriever(tmp_path / f"{arm}.db", arm, provider=Embeddings())
    adapter.ingest([{"sourceId": f"s{i:02}", "videoId": "v", "text": f"cup object {i}"} for i in range(30)])
    assert len(adapter.retrieve(RetrievalQuery("cup"))["candidateIds"]) == 30
    assert len(adapter.retrieve(RetrievalQuery("unrelated"))["candidateIds"]) == 30
    if arm == "A":
        assert adapter.retrieve(RetrievalQuery("unrelated"))["selectedIds"] == []


def test_query_and_ingest_boundary_excludes_all_gold_fields(tmp_path):
    sample = EgoLifeSample("key", "v", "s", "cup?", "SECRET ANSWER", {"frames": [42]}, "SECRET SOURCE")
    class Spy:
        name = "spy"
        def ingest(self, records):
            assert set(records[0]) == {"sourceId", "videoId", "text"}
        def retrieve(self, query):
            assert type(query) is RetrievalQuery
            assert vars(query) == {"question": "cup?"}
            return {"selected": [], "candidates": []}
    evaluate_adapter(Spy(), [{"sourceId": "s", "videoId": "v", "text": "source", "frames": [42]}], [sample])
    adapter = ControlledRetriever(tmp_path / "m.db", "B")
    with pytest.raises(TypeError):
        adapter.retrieve(sample)
    with pytest.raises(ValueError):
        adapter.ingest([{"sourceId": "s", "videoId": "v", "text": "t", "target": 42}])


def test_pure_rrf_does_not_depend_on_raw_similarity_scale():
    class Timeline:
        def retrieve(self, *args, **kwargs):
            return {"hardCandidates": [{"cardId": "a", "hardEligible": True}, {"cardId": "b", "hardEligible": True}],
                    "candidates": [{"cardId": "blocked", "rejectionReasons": ["time-mismatch"]}], "routing": {}}
    class Semantic:
        def __init__(self, scale): self.scale = scale
        def search(self, *args): return [("blocked", 1e20), ("b", self.scale), ("a", .5 * self.scale)]
    outputs = [EvidenceFirstPolicy(Timeline(), Semantic(s)).retrieve("q", user_id="u", case_id="c") for s in (1, 10000)]
    assert [x["cardId"] for x in outputs[0].selected] == [x["cardId"] for x in outputs[1].selected]
    assert {x["cardId"] for x in outputs[0].selected} == {"a", "b"}
    assert outputs[0].confidence is None
    assert rrf(["a", "a", "b"])["a"] == 1 / 61


def test_legacy_audit_provider_fails_closed():
    class Old:
        def retrieve(self, *args, **kwargs):
            return {"candidates": [{"cardId": "bad", "rejectionReasons": ["time-mismatch"]}]}
    assert not EvidenceFirstPolicy(Old()).retrieve("q", user_id="u", case_id="c").selected


def test_semantic_unavailable_does_not_lower_timeline_selection_floor():
    class Timeline:
        def retrieve(self, *args, **kwargs):
            return {"hardCandidates": [{"cardId": "irrelevant", "hardEligible": True}], "selected": []}
    class Broken:
        def search(self, *args): raise RuntimeError("unavailable")
    assert EvidenceFirstPolicy(Timeline(), Broken()).retrieve("q", user_id="u", case_id="c").selected == []


def test_baseline_uses_timeline_default_selection(tmp_path):
    adapter = ControlledRetriever(tmp_path / "a.db", "A")
    adapter.ingest([{"sourceId": str(i), "videoId": "v", "text": text} for i, text in enumerate(
        ["cup on the desk", "cup in a cupboard", "cup near the chair", "a blue cup", "a green cup", "totally irrelevant"])])
    for question in ("cup", "desk", "nothing matches"):
        expected = adapter.memory.retrieve(question, user_id="egolife", case_id="dataset", scope="case", now=FROZEN_CLOCK)
        assert adapter.retrieve(RetrievalQuery(question))["selectedIds"] == [r["cardId"] for r in expected["selected"]]


def test_bm25_document_frequency_and_fragment_provenance():
    documents = {"a": "common rare", "b": "common other", "c": "common extra"}
    index = BM25(documents)
    assert index.scores("rare", ["a"])["a"] > index.scores("common", ["a"])["a"]
    text = "First cup. 第二句在这里！ Third line?"
    for span in fragments("s", text).values():
        assert span["text"] == text[span["start"]:span["end"]]
        assert span["sourceId"] == "s"


def test_empty_selection_never_falls_back_to_gold_in_candidate_audit():
    assert ranking_metrics([{"goldSourceId": "gold", "selectedIds": [], "candidateIds": ["gold"]}])["mrr"] == 0
    row = {"unavailable": False, "goldSourceId": "gold", "candidateIds": ["gold"], "selectedIds": [], "rankedIds": [], "retrievalMs": 1}
    assert metrics([row])["recall@1"] == 0
    assert metrics([{**row, "unavailable": True}])["recall@1"] is None


def test_calibration_has_no_validation_input_and_empty_coverage_is_not_success():
    rows = [{"key": str(i), "scores": [1, .1], "answered": True, "answerStatus": "measured", "evidenceSupport": 1} for i in range(5)]
    policy = calibrate(rows)
    assert policy["threshold"] == 0
    assert policy["calibrationKeys"] == [str(i) for i in range(5)]
    assert calibrate([{**r, "evidenceSupport": 0} for r in rows])["threshold"] is None
    assert apply_selection(rows[0], None)["answered"] is False


def test_splits_are_video_disjoint_and_shared_sources_fail_closed():
    samples = [EgoLifeSample(str(i), f"v{i}", f"s{i}", "q", "a", {}, "t") for i in range(9)]
    for seed in (0, 1, 2):
        parts = experiment_splits(samples, seed)
        assert set(parts) == {"development", "calibration", "reused_validation"}
        assert len({s.key for p in parts.values() for s in p}) == 9
    repeated = [EgoLifeSample(s.key, s.video_id, "same", s.question, s.expected, s.target, s.source) for s in samples]
    with pytest.raises(ValueError):
        experiment_splits(repeated, 0)


def test_embedding_response_order_and_role_specific_cache(tmp_path):
    provider = Provider(tmp_path, enabled=False)
    calls = []
    def request(endpoint, body):
        calls.append(body)
        return {"output": {"embeddings": [{"text_index": 1, "embedding": [0, 1]}, {"text_index": 0, "embedding": [1, 0]}]}}
    provider.request = request
    assert provider.encode(["a", "b"], "document").tolist() == [[1, 0], [0, 1]]
    provider.encode(["a", "b"], "query")
    provider.encode(["a", "b"], "query")
    assert len(calls) == 2
