from datetime import datetime, timedelta, timezone

from egoassist.core.journal import SessionJournal
from egoassist.memory.timeline import TimelineMemory
from egoassist.product.answer_jobs import answer_jobs
from egoassist.realtime.work_ledger import CaseWorkLedger
from egoassist.runtime import ThreeLaneRuntime


def test_long_journal_projection_consumes_only_new_events_and_resets(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    for index in range(3000):
        journal.append("runtime_event", {"sequence": index})
    seen = []

    def reduce(state, row):
        seen.append(row["id"])
        state["count"] += 1

    assert journal.project("count", lambda: {"count": 0}, reduce)["count"] == 3000
    cursor, rows, _ = journal.read_since()
    assert len(rows) == 3000
    for _ in range(10):
        journal.project("count", dict, reduce)
    assert len(seen) == 3000
    journal.append("answer.job", {"turnId": "turn", "status": "queued"})
    assert journal.project("count", dict, reduce)["count"] == 3001
    assert len(seen) == 3001
    assert len(journal.read_since(cursor)[1]) == 1
    other = SessionJournal(journal.path)
    other.rewrite(lambda rows: rows[-1:])
    assert journal.project("count", lambda: {"count": 0}, reduce)["count"] == 1
    assert journal.read_since(cursor)[2] is True


def test_hot_read_models_do_not_request_full_history(tmp_path, monkeypatch):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    ledger = CaseWorkLedger(journal)
    ledger.capture_turn("question", source="test", turn_id="turn")
    journal.append("answer.job", {"turnId": "turn", "status": "queued"})
    runtime = ThreeLaneRuntime(journal, "user")
    original = journal.entries

    def bounded(*, types=None, tail=None):
        assert types is not None or tail is not None, (
            "hot path requested the entire event history"
        )
        return original(types=types, tail=tail)

    monkeypatch.setattr(journal, "entries", bounded)
    assert answer_jobs(journal)["turn"]["status"] == "queued"
    assert ledger.turns()["turn"]["text"] == "question"
    assert (
        runtime.snapshot(observation={}, scheduler={})["interaction"]["answers"][
            "queued"
        ]
        == 1
    )


def test_bounded_retrieval_finds_old_exact_and_lexical_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOASSIST_MEMORY_CANDIDATE_LIMIT", "32")
    memory = TimelineMemory(tmp_path / "timeline.sqlite3")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    exact = memory.remember(
        "special locker number 729",
        user_id="u",
        case_id="case",
        scope="case",
        kind="fact",
        subject="locker",
        predicate="number",
        recorded_at=old,
    )
    for index in range(120):
        memory.remember(
            f"unrelated observation {index}",
            user_id="u",
            case_id="case",
            scope="case",
            kind="fact",
        )
    other = memory.remember(
        "special locker number 888",
        user_id="other",
        case_id="case",
        scope="case",
        kind="fact",
    )
    restored = TimelineMemory(memory.path)
    for spec in ({}, {"subject": "locker", "predicate": "number"}):
        result = restored.retrieve(
            "special locker number",
            user_id="u",
            case_id="case",
            scope="case",
            query_spec=spec,
        )
        assert result["timing"]["scannedCards"] <= 32
        assert result["timing"]["totalCandidates"] == 121
        assert result["timing"]["candidatesTruncated"] is True
        assert exact["id"] in [row["cardId"] for row in result["selected"]]
        assert other["id"] not in str(result)
