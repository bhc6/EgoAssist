import base64
from datetime import datetime, timedelta, timezone

import pytest

from egoassist.realtime import SessionJournal, CaseWorkLedger
from egoassist.realtime.interaction import InteractionState, normalize_interaction


def test_quiet_survives_recreation_and_new_question_does_not_release_proactive(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    state = InteractionState(journal)
    state.apply({"audience": "assistant", "output": "silent", "quiet": {"mode": "set", "seconds": 300}}, "quiet")
    recovered = InteractionState(journal)
    assert recovered.snapshot()["quiet"]
    recovered.apply({"audience": "assistant", "action": "reply", "output": "speech"}, "question")
    assert recovered.allows({"turnId": "question"})
    assert not recovered.allows({}, "proactive-utterance")
    recovered.pause()
    assert not recovered.allows({"turnId": "question"})
    recovered.apply({"audience": "assistant", "output": "speech", "quiet": {"mode": "clear"}}, "release")
    assert recovered.allows({}, "proactive-utterance")


def test_duration_anchors_to_user_turn_not_recovery_time(tmp_path):
    state = InteractionState(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    state.apply({"audience": "assistant", "action": "listen", "output": "silent", "quiet": {"mode": "set", "seconds": 30}}, "quiet",
                timestamp=(datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat())
    assert not state.snapshot()["quiet"]


def test_unresolved_speech_holds_output_after_restart(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    state = InteractionState(journal)
    state.pause()
    recovered = InteractionState(journal)
    assert not recovered.allows({}, "proactive-utterance")
    recovered.apply({"audience": "assistant", "action": "reply", "output": "speech"}, "new")
    assert recovered.allows({"turnId": "new"})
    assert not InteractionState(journal).paused


def test_answer_revision_keeps_independent_jobs(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    ledger.capture_turn("Explain a concept and remind me later", source="test", sequence=1, turn_id="original")
    ledger.capture_turn("The answer is unnecessary", source="test", sequence=2, turn_id="cancel")
    answer = ledger.create_intent("original", kind="foreground-dialogue")
    reminder = ledger.create_intent("original", kind="background-reminder", intent_id="reminder")
    ledger.create_job(answer["intentId"], kind="foreground-dialogue", job_id="answer-job")
    ledger.create_job(reminder["intentId"], kind="background-reminder", job_id="reminder-job")
    ledger.revise_turn("cancel", "original", revision_type="cancellation", scope="answer")
    assert ledger.jobs()["answer-job"]["status"] == "cancelled"
    assert ledger.jobs()["reminder-job"]["status"] != "cancelled"


def test_missing_cancel_target_never_substitutes_current_question():
    context = {"previousAnswer": {"turnId": "a"}}
    result = normalize_interaction({"action": "cancel", "output": "silent"}, context, "cancel older answer")
    assert result["action"] == "uncertain"
    with pytest.raises(ValueError):
        normalize_interaction({"action": "cancel", "targetTurnId": "missing"}, context, "cancel")
    result = normalize_interaction({"audience": "other", "action": "cancel", "targetTurnId": "a"}, context, "another person")
    assert result["action"] == "listen" and "targetTurnId" not in result


def test_playback_tail_uses_device_cursor_not_generated_transcript(tmp_path):
    state = InteractionState(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    pcm = bytes(range(200))
    state.observe({"type": "audio", "responseId": "response", "origin": {"turnId": "question"},
                   "audio": base64.b64encode(pcm).decode()})
    assert state.progress({"responseId": "response", "playedSamples": 40})
    assert state.tail("question")[2] == pcm[80:]
    assert not state.progress({"responseId": "response", "playedSamples": 101})
    assert state.progress({"responseId": "response", "playedSamples": 10})
    assert state.tail("question")[2] == pcm[80:]
