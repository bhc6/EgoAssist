import pytest

from egoassist.realtime import CaseWorkLedger, SessionJournal


def test_turn_inbox_persists_text_before_processing_and_recovers_message(tmp_path):
    path = tmp_path / "case.jsonl"
    journal = SessionJournal(path, session_id="case")
    ledger = CaseWorkLedger(journal)

    captured = ledger.capture_turn(
        "provider completed this transcript",
        source="input-audio-transcript",
        sequence=7,
        provider_item_id="provider-item-7",
    )
    ledger.set_turn_status(captured["turnId"], "classifying")

    restored = CaseWorkLedger(SessionJournal(path, session_id="case"))
    pending = restored.recoverable_turns()
    assert [(item["turnId"], item["text"], item["status"]) for item in pending] == [
        ("provider-item-7", "provider completed this transcript", "classifying"),
    ]
    messages = [
        item for item in restored.journal.entries()
        if item.get("type") == "message" and item.get("message", {}).get("role") == "user"
    ]
    assert len(messages) == 1
    assert messages[0]["message"]["content"] == "provider completed this transcript"


def test_three_ledgers_restore_focus_jobs_and_pending_results(tmp_path):
    path = tmp_path / "case.jsonl"
    ledger = CaseWorkLedger(SessionJournal(path, session_id="case"))
    turn = ledger.capture_turn("remind me after the rice is done", source="test", sequence=1)
    intent = ledger.create_intent(
        turn["turnId"],
        kind="background-reminder",
        skill_ref={"name": "kitchen-coach", "version": "1"},
        status="running-background",
    )
    ledger.focus_intent(intent["intentId"], reason="accepted")
    job = ledger.create_job(intent["intentId"], kind="timer", status="running")
    ledger.set_job_status(job["jobId"], "completed")
    result = ledger.enqueue_result(
        intent["intentId"], "The rice timer finished.", job_id=job["jobId"], priority=90,
    )

    restored = CaseWorkLedger(SessionJournal(path, session_id="case"))
    snapshot = restored.snapshot()
    assert snapshot["foregroundIntentId"] == intent["intentId"]
    assert snapshot["jobs"][0]["status"] == "completed"
    assert restored.pending_results()[0]["resultId"] == result["resultId"]

    restored.set_result_status(result["resultId"], "delivered")
    assert restored.pending_results() == []


def test_terminal_turn_cannot_be_reopened(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    turn = ledger.capture_turn("done", source="test")
    ledger.set_turn_status(turn["turnId"], "completed")
    with pytest.raises(ValueError, match="terminal turn"):
        ledger.set_turn_status(turn["turnId"], "processing")


def test_result_inbox_keeps_multiple_results_until_each_is_delivered(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    turn = ledger.capture_turn("start two independent jobs", source="test")
    intent = ledger.create_intent(turn["turnId"], kind="background-work", status="running-background")
    first = ledger.enqueue_result(intent["intentId"], "first", priority=50)
    second = ledger.enqueue_result(intent["intentId"], "second", priority=50)

    assert [item["resultId"] for item in ledger.pending_results()] == [first["resultId"], second["resultId"]]
    ledger.set_result_status(first["resultId"], "delivered")
    assert [item["resultId"] for item in ledger.pending_results()] == [second["resultId"]]


def test_result_arbiter_skips_suspended_stale_intent(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    old_turn = ledger.capture_turn("old topic", source="test", sequence=1)
    old_intent = ledger.create_intent(old_turn["turnId"], kind="background-work", status="running-background")
    old_result = ledger.enqueue_result(old_intent["intentId"], "old result", priority=100)
    new_turn = ledger.capture_turn("new topic", source="test", sequence=2)
    ledger.suspend_before_sequence(2)

    assert ledger.claim_next_result(latest_sequence=2) is None


def test_background_envelope_carries_session_and_generation(tmp_path):
    journal = SessionJournal(tmp_path / "session.jsonl", session_id="envelope")
    ledger = CaseWorkLedger(journal)
    turn = ledger.capture_turn("run background work", source="test", sequence=1)
    intent = ledger.create_intent(
        turn["turnId"], kind="background-job", status="running-background", generation=7,
    )
    job = ledger.create_job(intent["intentId"], kind="background-job", status="waiting-result")
    result = ledger.enqueue_result(intent["intentId"], "done", job_id=job["jobId"])
    assert result["correlation"] == {
        "sessionId": "envelope",
        "turnId": turn["turnId"],
        "intentId": intent["intentId"],
        "jobId": job["jobId"],
        "resultId": result["resultId"],
        "generation": 7,
    }


def test_stale_scheduled_result_can_be_delivered_when_explicitly_allowed(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    old_turn = ledger.capture_turn("old reminder", source="test", sequence=1)
    intent = ledger.create_intent(old_turn["turnId"], kind="background-reminder", status="running-background")
    result = ledger.enqueue_result(intent["intentId"], "reminder fired", allow_stale=True, priority=90)
    ledger.capture_turn("new topic", source="test", sequence=2)

    assert ledger.claim_next_result(latest_sequence=2)["resultId"] == result["resultId"]


def test_due_jobs_are_recoverable_after_disconnect(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    turn = ledger.capture_turn("remind me", source="test")
    intent = ledger.create_intent(turn["turnId"], kind="background-reminder", status="running-background")
    job = ledger.create_job(
        intent["intentId"],
        kind="timer-delivery",
        payload={"dueAt": "2020-01-01T00:00:00+00:00", "label": "rice"},
        status="queued",
    )
    restored = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    assert [item["jobId"] for item in restored.due_jobs()] == [job["jobId"]]


def test_abandoned_speech_claim_is_recovered_without_losing_other_results(tmp_path):
    path = tmp_path / "case.jsonl"
    ledger = CaseWorkLedger(SessionJournal(path, session_id="case"))
    turn = ledger.capture_turn("two results", source="test")
    intent = ledger.create_intent(turn["turnId"], kind="background-work", status="running-background")
    first = ledger.enqueue_result(intent["intentId"], "first", priority=90)
    second = ledger.enqueue_result(intent["intentId"], "second", priority=80)
    assert ledger.claim_next_result()["resultId"] == first["resultId"]

    restored = CaseWorkLedger(SessionJournal(path, session_id="case"))
    assert restored.recover_delivery_claims() == 1
    assert [item["resultId"] for item in restored.pending_results()] == [
        first["resultId"], second["resultId"],
    ]


def test_explicit_job_cancel_only_cancels_its_intent_when_no_work_remains(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    turn = ledger.capture_turn("two jobs", source="test")
    intent = ledger.create_intent(turn["turnId"], kind="background-work", status="running-background")
    first = ledger.create_job(intent["intentId"], kind="one", status="running")
    second = ledger.create_job(intent["intentId"], kind="two", status="running")
    ledger.cancel_job(first["jobId"])
    assert ledger.intents()[intent["intentId"]]["status"] == "running-background"
    ledger.cancel_job(second["jobId"])
    assert ledger.intents()[intent["intentId"]]["status"] == "cancelled"


def test_focus_switch_suspends_previous_foreground_intent(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    first_turn = ledger.capture_turn("first task", source="test", sequence=1)
    first = ledger.create_intent(
        first_turn["turnId"], kind="foreground-dialogue", status="foreground",
    )
    ledger.focus_intent(first["intentId"], reason="first")
    second_turn = ledger.capture_turn("second task", source="test", sequence=2)
    second = ledger.create_intent(
        second_turn["turnId"], kind="foreground-dialogue", status="foreground",
    )
    ledger.focus_intent(second["intentId"], reason="topic-switch")
    assert ledger.intents()[first["intentId"]]["status"] == "suspended"
    assert ledger.foreground_intent_id() == second["intentId"]


def test_memory_draft_continuation_links_new_turn_to_existing_intent(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    first_turn = ledger.capture_turn("remember a phone number", source="test", sequence=1)
    intent = ledger.create_intent(
        first_turn["turnId"],
        kind="waiting-input",
        payload={"semantic": {"memoryDraft": {"mode": "start"}}},
        status="waiting-input",
    )
    followup = ledger.capture_turn("it belongs to Li", source="test", sequence=2)
    ledger.link_turn_to_intent(intent["intentId"], followup["turnId"])

    restored = ledger.intents()[intent["intentId"]]
    assert restored["turnIds"] == [first_turn["turnId"], followup["turnId"]]
    assert ledger.journal.entries()[-1]["type"] == "intent_turn_link"


def test_turn_revision_cancels_superseded_work(tmp_path):
    ledger = CaseWorkLedger(SessionJournal(tmp_path / "case.jsonl", session_id="case"))
    old_turn = ledger.capture_turn("check Beijing", source="test", sequence=1)
    intent = ledger.create_intent(old_turn["turnId"], kind="foreground-dialogue", status="foreground")
    job = ledger.create_job(intent["intentId"], kind="lookup", status="running")
    new_turn = ledger.capture_turn("not Beijing, Shanghai", source="test", sequence=2)

    ledger.revise_turn(new_turn["turnId"], old_turn["turnId"])

    assert ledger.turns()[old_turn["turnId"]]["supersededBy"] == new_turn["turnId"]
    assert ledger.intents()[intent["intentId"]]["status"] == "cancelled"
    assert ledger.jobs()[job["jobId"]]["status"] == "cancelled"
    assert ledger.turn_message(old_turn["turnId"])["message"]["content"] == "check Beijing"
