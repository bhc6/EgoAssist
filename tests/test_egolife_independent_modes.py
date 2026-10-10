import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("srt", reason="Install the egolife extra for official subtitle tests")


@pytest.fixture
def experiment(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("independent_modes", scripts / "egolife_independent_modes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_question_drops_gold_targets_and_answer_rationale(experiment):
    row = {"ID": "synthetic", "query_time": {"date": "DAY2", "time": "11223344"},
           "question_chinese": "where?", "answer": "A", "target_time": {"date": "DAY2", "time": "10000000"},
           "keywords": "gold-secret", "reason": "gold-secret", **{f"choice_{c}_chinese": c for c in "abcd"}}
    public = experiment.public_question(row)
    assert set(public) == {"id", "day", "queryClock", "cutoff", "question", "options"}
    assert "gold-secret" not in experiment.question_prompt(public)
    with pytest.raises(ValueError, match="non-public"):
        experiment.question_prompt({**public, "target": row["target_time"]})


def test_dataset_clock_is_conservative_and_rejects_invalid_time(experiment):
    assert experiment.clock_seconds("11223399") == 11 * 3600 + 22 * 60 + 33
    assert experiment.absolute_seconds("DAY2", 0) == 86400
    with pytest.raises(ValueError):
        experiment.clock_seconds("25609900")


def test_source_intervals_use_file_hour_plus_srt_offsets(experiment):
    payload = b"1\n00:00:02,500 --> 00:00:04,500\nUnidentified speaker says hello.\n\n"
    record = list(experiment.source_records("EgoLifeCap/Transcript/A1_JAKE/DAY2/A1_JAKE_DAY2_10000000.srt", payload))[0]
    assert record["start"] == 86400 + 36000 + 2.5
    assert record["end"] == 86400 + 36000 + 4.5
    assert record["kind"] == "transcript"
    assert record["text"] == "Unidentified speaker says hello."


def test_choice_parser_does_not_infer_option_from_explanation(experiment):
    assert experiment.parse_choice("A.") == "A"
    assert experiment.parse_choice("ABSTAIN") == "ABSTAIN"
    assert experiment.parse_choice("It could be A or B") is None
    assert experiment.parse_choice("") is None


def test_cross_hour_caption_keeps_full_end_time(experiment):
    payload = b"1\n00:59:59,600 --> 01:00:02,800\nThe cup was moved.\n\n"
    record = list(experiment.source_records("EgoLifeCap/DenseCaption/A1_JAKE/DAY2/A1_JAKE_DAY2_10000000.srt", payload))[0]
    assert record["end"] == 86400 + 36000 + 3602.8


def test_retrieval_never_indexes_unfinished_future_source(experiment, tmp_path):
    dataset, output = tmp_path / "data", tmp_path / "run"
    dataset.mkdir()
    output.mkdir()
    records = [{"sourceId": sid, "videoId": "DAY2:00", "text": text, "kind": "caption", "start": 86400,
                "end": end, "day": "DAY2", "path": sid + ".srt", "cue": 1}
               for sid, end, text in [("past", 86401, "The cup is on the desk."), ("future", 86403, "The cup moved into the cabinet.")]]
    question = {"id": "synthetic", "day": "DAY2", "queryClock": "00000200", "cutoff": 86402,
                "question": "Where is the cup?", "options": {"A": "desk", "B": "cabinet", "C": "chair", "D": "floor"}}
    (dataset / "sources.json").write_text(json.dumps(records), encoding="utf-8")
    (dataset / "questions.json").write_text(json.dumps([question]), encoding="utf-8")
    result = experiment.retrieve_all(dataset, output)[0]
    assert result["visibleSources"] == 1
    assert "future" not in result["candidateIds"]
    assert "future" not in result["selectedIds"]


def test_event_unit_is_withheld_until_its_last_cue_ends(experiment, tmp_path):
    dataset, output = tmp_path / "data", tmp_path / "run"
    dataset.mkdir()
    output.mkdir()
    records = [{"sourceId": sid, "videoId": "DAY2:00", "text": text, "kind": "caption", "start": start,
                "end": end, "day": "DAY2", "path": sid + ".srt", "cue": 1}
               for sid, start, end, text in [("past", 86400, 86401, "The cup is on the desk."),
                                             ("future", 86401.5, 86403, "The cup moved into the cabinet.")]]
    question = {"id": "synthetic", "day": "DAY2", "queryClock": "00000200", "cutoff": 86402,
                "question": "Where is the cup?", "options": {"A": "desk", "B": "cabinet", "C": "chair", "D": "floor"}}
    (dataset / "sources.json").write_text(json.dumps(records), encoding="utf-8")
    (dataset / "questions.json").write_text(json.dumps([question]), encoding="utf-8")
    result = experiment.retrieve_all(dataset, output, "event")[0]
    # Both cues form one unit that ends after the cutoff, so nothing is visible yet.
    assert result["visibleSources"] == 0
    assert result["candidateSourceIds"] == [] and result["selectedSourceIds"] == []
