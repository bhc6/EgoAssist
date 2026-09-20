import importlib.util
from pathlib import Path


def load_backtest(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("paired_backtest", scripts / "backtest_realtime_modes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_grade_rejects_wrong_identity_missing_evidence_and_source_instruction(monkeypatch):
    module = load_backtest(monkeypatch)
    cases = {case["id"]: case for case in module.CASES}
    assert not module.grade(cases["identity"], "In the drawer.")["passed"]
    assert not module.grade(cases["insufficient"], "On the desk.")["passed"]
    assert module.grade(cases["insufficient"], "I don't know.")["passed"]
    assert not module.grade(cases["source_boundary"], "BANANA")["passed"]
    assert not module.grade(cases["location"], "I don't know.")["passed"]


def test_transcription_metric_normalizes_punctuation_but_keeps_content_errors(monkeypatch):
    module = load_backtest(monkeypatch)
    assert module.normalized("Cup, DESK!") == module.normalized("cup desk")
    assert module.edit_distance("kitten", "sitting") == 3
    assert module.edit_distance(module.normalized("on the desk"), module.normalized("in the drawer")) > 0


def test_summary_keeps_failures_in_coverage_denominator(monkeypatch):
    module = load_backtest(monkeypatch)
    rows = []
    for backend in ("modular", "omni"):
        rows.extend([
            {"seed": 17, "backend": backend, "case": "location", "passed": True, "status": "completed", "answer": "desk", "grade": {"passed": True, "abstained": False}, "providerCalls": 2},
            {"seed": 17, "backend": backend, "case": "identity", "passed": False, "error": "timeout", "providerCalls": 1},
        ])
    result = module.summarize(rows)
    assert result["modular"]["answerCoverage"] == .5
    assert result["omni"]["qaTotal"] == 2
    assert result["omni"]["providerCalls"] is None
