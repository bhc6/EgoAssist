import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from egoassist.product.answer_jobs import AnswerJobs, answer_jobs
from egoassist.realtime import CaseWorkLedger, SessionJournal


@asynccontextmanager
async def running(manager):
    worker = asyncio.create_task(manager.run())
    try:
        yield
    finally:
        await manager.close()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def capture(ledger, turn_id, sequence):
    ledger.capture_turn("question " + turn_id, source="test", turn_id=turn_id, sequence=sequence)
    ledger.set_turn_status(turn_id, "completed")


async def wait_status(journal, turn_id, status):
    async with asyncio.timeout(5):
        while answer_jobs(journal)[turn_id]["status"] != status:
            await asyncio.sleep(0.01)
    return answer_jobs(journal)[turn_id]


def test_prepare_answer_during_slow_correction_without_publishing_or_regenerating(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOASSIST_ANSWER_CORRECTION_GRACE", "0.1")

    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        ledger = CaseWorkLedger(journal)
        capture(ledger, "old", 1)
        calls = []

        async def generate(journal, row):
            calls.append(row["turnId"])
            return {"text": "prepared once"}

        manager = AnswerJobs("unused", generate=generate)
        manager.register("u", journal, turn_id="old", question="old", instructions="evidence", deferred=True)
        ledger.capture_turn("new", source="test", turn_id="new", sequence=2)
        async with running(manager):
            async with asyncio.timeout(3):
                while not answer_jobs(journal)["old"].get("preparedResult"):
                    await asyncio.sleep(0.01)
            assert "preparedResult" not in ledger.snapshot()["turnInbox"][0]["answer"]
            assert answer_jobs(journal)["old"]["status"] == "queued"
            generation_ms = answer_jobs(journal)["old"]["generationMs"]
            ledger.set_turn_status("new", "completed")
            result = await wait_status(journal, "old", "completed")
            assert result["text"] == "prepared once" and calls == ["old"]
            assert result["durationMs"] == generation_ms
            snapshot = ledger.snapshot()["turnInbox"][0]["answer"]
            assert snapshot["inputToCompleteMs"] >= generation_ms

    asyncio.run(run())


def test_interrupted_answers_complete_once_without_reprocessing_turns(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        ledger = CaseWorkLedger(journal)
        calls = []

        async def generate(journal, row):
            calls.append(row["turnId"])
            return {"text": "answer " + row["turnId"], "providerRequestId": "test-result"}

        manager = AnswerJobs("unused", generate=generate)
        for index, turn_id in enumerate(("first", "second")):
            capture(ledger, turn_id, index)
            intent = ledger.create_intent(turn_id, kind="foreground-dialogue", status="suspended")
            job = ledger.create_job(intent["intentId"], kind="foreground-dialogue", status="suspended")
            manager.register("u", journal, turn_id=turn_id, question=turn_id, instructions="grounded snapshot",
                             dialogue_job={"intentId": intent["intentId"], "jobId": job["jobId"]})
        manager.interrupt(journal, "first")
        manager.foreground_finished(journal, "second", "spoken-second")
        journal.append_message("assistant", "second answer", source="provider-output", response_id="spoken-second")
        async with running(manager):
            result = await wait_status(journal, "first", "completed")
            assert result["text"] == "answer first"
            assert result["channel"] == "background-text"
            assert calls == ["first"]
            assert len(ledger.turns()) == 2
            assert ledger.turns()["first"]["status"] == "completed"
            assert "instructions" not in ledger.snapshot()["turnInbox"][0]["answer"]
            assert ledger.snapshot()["turnInbox"][1]["answer"]["text"] == "second answer"
            assert next(i for i in ledger.intents().values() if i["turnId"] == "first")["status"] == "completed"

    asyncio.run(run())


def test_correction_waits_for_router_then_cancels_old_answer(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        ledger = CaseWorkLedger(journal)
        calls = []

        async def generate(journal, row):
            calls.append(row["turnId"])
            return {"text": "must not be generated"}

        capture(ledger, "first", 1)
        manager = AnswerJobs("unused", generate=generate)
        manager.register("u", journal, turn_id="first", question="first", instructions="old", deferred=True)
        ledger.capture_turn("correction", source="test", turn_id="correction", sequence=2)
        async with running(manager):
            await asyncio.sleep(0.3)
            assert not calls
            ledger.revise_turn("correction", "first")
            ledger.set_turn_status("correction", "completed")
            await wait_status(journal, "first", "cancelled")
            assert not calls

    asyncio.run(run())


def test_cancelled_inflight_answer_cannot_publish_late_result(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        ledger = CaseWorkLedger(journal)
        capture(ledger, "first", 1)
        started = asyncio.Event()

        async def generate(journal, row):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return {"text": "late result"}

        manager = AnswerJobs("unused", generate=generate)
        manager.register("u", journal, turn_id="first", question="first", instructions="old", deferred=True)
        async with running(manager):
            await asyncio.wait_for(started.wait(), 3)
            manager.cancel(journal, "first")
            await asyncio.gather(*list(manager.tasks.values()), return_exceptions=True)
            result = answer_jobs(journal)["first"]
            assert result["status"] == "cancelled" and "text" not in result

    asyncio.run(run())


def test_shutdown_restart_recovers_answer_without_replaying_controls(tmp_path):
    async def run():
        path = tmp_path / "case.jsonl"
        journal = SessionJournal(path, session_id="case")
        ledger = CaseWorkLedger(journal)
        capture(ledger, "first", 1)

        async def blocked(journal, row):
            await asyncio.Future()

        manager = AnswerJobs("unused", generate=blocked)
        manager.register("u", journal, turn_id="first", question="first", instructions="old", deferred=True)
        async with running(manager):
            await wait_status(journal, "first", "running")
        assert answer_jobs(journal)["first"]["status"] == "queued"

        async def generate(journal, row):
            return {"text": "recovered answer"}

        restored = SessionJournal(path, session_id="case")
        manager = AnswerJobs("unused", generate=generate)
        manager.recover("u", restored)
        async with running(manager):
            result = await wait_status(restored, "first", "completed")
            assert result["attempt"] == 2 and result["text"] == "recovered answer"
            assert len(CaseWorkLedger(restored).turns()) == 1

    asyncio.run(run())


def test_failed_answer_does_not_retry_until_requested(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        ledger = CaseWorkLedger(journal)
        capture(ledger, "first", 1)
        count = 0

        async def generate(journal, row):
            nonlocal count
            count += 1
            if count == 1:
                raise TimeoutError("provider timeout")
            return {"text": "successful retry"}

        manager = AnswerJobs("unused", generate=generate)
        manager.register("u", journal, turn_id="first", question="first", instructions="old", deferred=True)
        async with running(manager):
            await wait_status(journal, "first", "failed")
            await asyncio.sleep(0.3)
            assert count == 1
            manager.retry("u", journal, "first")
            await wait_status(journal, "first", "completed")
            assert count == 2 and len(ledger.turns()) == 1

    asyncio.run(run())


def test_abandoned_new_turn_does_not_block_old_answer_forever(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
        ledger = CaseWorkLedger(journal)
        capture(ledger, "first", 1)
        ledger.capture_turn("unprocessed", source="test", turn_id="second", sequence=2)
        ledger.journal.append("turn_inbox_state", {"turnId": "second", "status": "received",
            "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()})

        async def generate(journal, row):
            assert row["context"]["retrievalEvidence"][0]["text"] == "The code is 7392."
            return {"text": "7392"}

        manager = AnswerJobs("unused", generate=generate)
        manager.register("u", journal, turn_id="first", question="What is the code?", instructions="Use evidence",
                         context={"retrievalEvidence": [{"text": "The code is 7392."}]}, deferred=True)
        async with running(manager):
            result = await wait_status(journal, "first", "completed")
            assert result["text"] == "7392"
            assert ledger.turns()["second"]["status"] == "received"
            assert "context" not in ledger.snapshot()["turnInbox"][0]["answer"]

    asyncio.run(run())
