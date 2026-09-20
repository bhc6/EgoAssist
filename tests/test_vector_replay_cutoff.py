import importlib.util
from pathlib import Path

from egoassist.memory import TimelineMemory


def test_replay_preserves_source_ending_exactly_at_question_cutoff(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("vector_experiment", scripts / "test_egolife_vector_retrieval.py")
    experiment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(experiment)
    memory = TimelineMemory(tmp_path / "timeline.db")
    for sid, end in [("before", 338216.999), ("equal", 338217), ("after", 338217.001)]:
        memory.remember("Alice cup " + sid, user_id="u", case_id="c", scope="case", kind="event",
                        card_id=sid, recorded_at=experiment.synthetic_time(end), source_ids=[sid])
    experiment.rewind_to_cutoff(memory, 338217)
    assert {r["id"] for r in memory.cards(user_id="u", case_id="c", scope="case")} == {"before", "equal"}
