import importlib
import math
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture
def experiment(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("compare_egolife_mcq")


def test_future_removal_matches_fresh_past_only_bm25(experiment):
    docs = {"past1": "cup desk cup", "past2": "cup chair", "future": "cup desk cup secret"}
    for chars in (False, True):
        index = experiment.TemporalBM25(docs, chars=chars)
        index.remove(["future"])
        fresh = experiment.BM25({k: v for k, v in docs.items() if k != "future"}, chars=chars)
        assert index.scores("cup desk", ["past1", "past2"]) == fresh.scores("cup desk", ["past1", "past2"])


def test_answer_prompt_includes_options_and_rejects_private_fields(experiment):
    query = {"id": "q", "day": "DAY2", "queryClock": "12000000", "cutoff": 129600,
             "question": "Where is the cup?", "options": dict(zip("ABCD", ["desk", "chair", "floor", "shelf"]))}
    source = {"s": {"text": "cup on desk", "day": "DAY2", "kind": "caption", "start": 129000}}
    payload = experiment.answer_payload(query, {"selectedIds": ["s"]}, source)
    assert payload["options"] == query["options"]
    assert payload["evidence"][0]["text"] == source["s"]["text"]
    with pytest.raises(ValueError, match="non-public"):
        experiment.answer_payload({**query, "gold": "A"}, {"selectedIds": ["s"]}, source)


def test_multiple_relevant_sources_use_normalized_dcg(experiment):
    row = {"rankedIds": ["x", "a", "b"], "selectedIds": ["x", "a", "b"], "candidateIds": ["x", "a", "b"]}
    metrics = experiment.target_metrics(row, {"a", "b"})
    assert metrics["targetMRR"] == 0.5
    assert metrics["targetRecall5"] == 1
    assert metrics["targetNDCG5"] == pytest.approx((1 / math.log2(3) + 1 / math.log2(4)) / (1 + 1 / math.log2(3)))
    missing = experiment.target_metrics(row, set())
    assert missing["targetRecall5"] is None
    assert missing["targetNDCG5"] == 0


def test_all_rankers_stay_in_hard_pool(experiment):
    sources = {"safe": {"text": "cup on desk"}, "excluded": {"text": "cup cup cup on desk"}}
    texts = {k: s["text"] for k, s in sources.items()}
    spans, source_spans = {}, {}
    for sid, text in texts.items():
        parts = experiment.fragments(sid, text)
        spans.update(parts)
        source_spans[sid] = list(parts)
    word, char = experiment.BM25(texts), experiment.BM25(texts, chars=True)
    parts = {k: s["text"] for k, s in spans.items()}
    part_word, part_char = experiment.BM25(parts), experiment.BM25(parts, chars=True)
    base = {"hardCandidates": [{"cardId": "safe", "score": 1, "rejectionReasons": []}],
            "selected": [{"cardId": "safe"}], "minimumScore": .05}

    class Vectors:
        def encode(self, values, role):
            assert "cup cup cup on desk" not in values
            return np.ones((len(values), 2))

    for arm in experiment.ARMS:
        selected, ranked, provenance = experiment.rank_arm(arm, "cup", base, sources, word, char, spans,
                                                          source_spans, part_word, part_char, Vectors())
        assert selected == ranked == ["safe"]
        for sid, span in provenance.items():
            assert span["text"] == sources[sid]["text"][span["start"]:span["end"]]


def make_row(**overrides):
    row = dict(status="completed", choice="A", correct=True, support=True,
               embeddingCacheHits=0, retrievalMs=1, rankMs=1, answerMs=2, endToEndMs=3)
    return {**row, **overrides}


def test_api_failure_is_unknown_but_completed_invalid_answer_is_wrong(experiment):
    partial = experiment.aggregate([make_row(), make_row(status="unavailable", choice=None, correct=None, support=None)])
    assert partial["accuracy"] is None and partial["coverage"] is None
    assert partial["unavailableTrials"] == 1
    assert partial["accuracyOnCompleted"] == 1
    invalid = experiment.aggregate([make_row(), make_row(choice=None, correct=False, support=None)])
    assert invalid["accuracy"] == invalid["coverage"] == .5
    assert invalid["invalidFormat"] == 1
    assert invalid["recall5"] is None


def test_account_denial_stops_second_provider_call(experiment, monkeypatch, tmp_path):
    calls = []

    class Response:
        status_code = 400

        def json(self):
            return {"code": "Arrearage", "message": "sensitive provider message"}

    def post(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(experiment.requests, "post", post)
    client = experiment.Client(tmp_path)
    client.key = "fake"
    for _ in range(2):
        with pytest.raises(experiment.ProviderRequestError):
            client.call("answer", "system", {}, 17)
    assert len(calls) == 1
    assert "sensitive" not in (tmp_path / "calls.json").read_text()


def test_successful_malformed_json_stays_a_completed_format_error(experiment, monkeypatch, tmp_path):
    class Response:
        status_code = 200

        def json(self):
            return {"output": {"choices": [{"message": {"content": "I choose A"}}]}}

    monkeypatch.setattr(experiment.requests, "post", lambda *a, **kw: Response())
    client = experiment.Client(tmp_path)
    client.key = "fake"
    assert client.call("answer", "system", {}, 17) == "I choose A"
