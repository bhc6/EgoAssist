import asyncio
from datetime import datetime, timezone
import json

from egoassist.memory.recovery import MemoryRecovery, recovery_jobs
from egoassist.memory.temporal import ClockContext
from egoassist.memory.timeline import TimelineMemory
from egoassist.realtime.context_harness import SessionJournal
from egoassist.realtime.prompt_audit import audit_page
from egoassist.realtime.router_protocol import expand_decision
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.intent_decomposition import normalize_intent_candidates
from egoassist.realtime.settings import asr_settings


def test_compound_memory_and_intents_are_not_silently_truncated():
    text = "；".join(f"项目{i}未完成" for i in range(8))
    wire = {"v": 1, "memoryEvents": [
        {"text": f"项目{i}未完成", "scope": "participant", "consent": "explicit"} for i in range(8)]}
    atoms = SemanticTurnRouter._normalize_atoms(expand_decision(wire)["memoryAtoms"], text)
    assert len(atoms) == 8
    mixed = expand_decision({"v": 1, "memoryEvents": wire["memoryEvents"],
                             "memoryAtoms": [{"kind": "event", "text": "额外原话", "scope": "case", "consent": "observed"}]})
    assert len(mixed["memoryAtoms"]) == 9
    intents = [{"kind": "foreground-dialogue", "text": f"question {i}", "parameters": {}} for i in range(8)]
    assert len(normalize_intent_candidates(text, {"intents": SemanticTurnRouter._normalize_intents(intents, text)})) == 8
    long_text = "原话不应被截断。" * 300
    assert SemanticTurnRouter._normalize_atoms([{"kind": "event", "scope": "case", "consent": "observed"}], long_text)[0]["text"] == long_text


def test_audit_cursor_preserves_all_model_events_and_new_families():
    rows = [{"id": str(i), "type": "tool.request" if i % 2 else "future.request", "model": "actual"} for i in range(75)]
    rows.append({"id": "raw", "type": "runtime_event", "eventType": "perception.raw_output", "payload": {"rawOutput": "actual"}})
    ids, before = [], None
    while True:
        page = audit_page(rows, before=before, limit=17)
        ids.extend(row["id"] for row in page["requests"])
        before = page["nextBefore"]
        if before is None:
            break
    assert len(ids) == len(set(ids)) == 76


def test_asr_config_reports_actual_route(monkeypatch):
    monkeypatch.setenv("EGOASSIST_ASR_MODEL", "test-asr-route")
    monkeypatch.setenv("EGOASSIST_ASR_TIMEOUT", "12")
    monkeypatch.setenv("EGOASSIST_ASR_CONCURRENCY", "3")
    assert asr_settings() == {"model": "test-asr-route", "timeoutSeconds": 12, "concurrency": 3}


def test_memory_recovery_restart_is_idempotent_and_preserves_source_clock(tmp_path):
    async def scenario():
        timeline = TimelineMemory(tmp_path / "timeline.sqlite3")
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        policy = lambda *args: {"components": {"timeline": {"preference_enabled": True, "episodic_enabled": True}}}
        worker = MemoryRecovery("", timeline, policy)
        clock = ClockContext.current("Asia/Shanghai", now_utc=datetime(2026, 9, 8, 23, tzinfo=timezone.utc))
        text = "记住，今天没有吃过药。"
        worker.submit(journal, user_id="user", turn_id="turn", text=text, source_id="source", clock=clock.prompt_payload())
        row = recovery_jobs(journal)["turn"]
        worker.update(journal, row, status="running")
        restored = MemoryRecovery("", timeline, policy)
        restored.recover(journal)
        assert recovery_jobs(journal)["turn"]["status"] == "queued"
        async def classify(*args):
            return {"status": "classified", "componentErrors": [], "taskControl": "confirm-step",
                    "memoryAtoms": [{"kind": "event", "text": text, "scope": "participant", "consent": "explicit"}]}
        restored.router.classify = classify
        await restored.execute(journal, recovery_jobs(journal)["turn"])
        await restored.execute(journal, row)
        cards = timeline.cards(user_id="user", scope="participant")
        assert len(cards) == 1 and cards[0]["text"] == text
        assert cards[0]["predicate"] is None
        assert cards[0]["recordedAt"].startswith("2026-09-08")
        assert cards[0]["dayKey"] == "2026-09-09"
        assert not any(r["type"].startswith("tool") for r in journal.entries())
        assert recovery_jobs(journal)["turn"]["status"] == "completed"
        await restored.close()
        await worker.close()
    asyncio.run(scenario())
