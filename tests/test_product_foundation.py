import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from egoassist.product import (
    ConversationTurn,
    CorrelationIds,
    HumanHelpRequest,
    IntentCandidate,
    JobRecord,
    ProductDatabase,
    ResultRecord,
    correlation_ids,
    product_span,
)
from egoassist.product.models import ConversationTurnRow
from egoassist.product.artifacts import LocalArtifactAdapter
from egoassist.product.human_help import WebhookHumanHelpAdapter
from egoassist.realtime import CaseWorkLedger, SessionJournal


def _alembic_config(database_path: Path) -> Config:
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def test_contracts_round_trip_and_reject_untyped_provider_fields():
    now = datetime.now(timezone.utc)
    turn = ConversationTurn(
        turn_id="turn-1", user_id="user-1", session_id="case-1",
        text="remember this", source="test", recorded_at=now,
        correlation=CorrelationIds(turn_id="turn-1"),
    )
    assert ConversationTurn.model_validate(turn.model_dump()).turn_id == "turn-1"
    assert turn.model_dump(mode="json")["correlation"]["turn_id"] == "turn-1"
    with pytest.raises(ValueError):
        ConversationTurn.model_validate({**turn.model_dump(), "provider_secret": "must not be domain state"})

    job = JobRecord(
        job_id="job-1", intent_id="intent-1", executor_kind="test",
        idempotency_key="job-1", correlation=CorrelationIds(job_id="job-1"),
    )
    result = ResultRecord(
        result_id="result-1", intent_id="intent-1", content={"ok": True},
        correlation=CorrelationIds(job_id="job-1", result_id="result-1"),
    )
    request = HumanHelpRequest(
        request_id="help-1", user_id="user-1", intent_id="intent-1",
        connector="test", purpose="safety", consent_id="consent-1",
        idempotency_key="help-1",
    )
    candidate = IntentCandidate(
        kind="foreground-dialogue", text="answer this", parameters={},
    )
    assert job.job_id and result.result_id


def test_ports_are_small_protocol_boundaries():
    from egoassist.product.ports import (
        ArtifactPort, HumanHelpPort, InteractionTransport,
        NotificationPort, PerceptionPort, WorkflowPort,
    )
    assert all(getattr(port, "_is_runtime_protocol", False) for port in (
        InteractionTransport, WorkflowPort, NotificationPort,
        ArtifactPort, HumanHelpPort, PerceptionPort,
    ))


def test_alembic_upgrade_and_downgrade_are_reversible(tmp_path):
    path = tmp_path / "product.sqlite3"
    config = _alembic_config(path)
    command.upgrade(config, "head")
    tables = set(inspect(ProductDatabase(path).engine).get_table_names())
    assert {"users", "conversation_turns", "intents", "jobs", "results", "memory_cards", "observations"} <= tables
    command.downgrade(config, "base")
    assert inspect(ProductDatabase(path).engine).get_table_names() == ["alembic_version"]


def test_legacy_journal_projects_idempotently_without_mutating_source(tmp_path):
    journal_path = tmp_path / "legacy.jsonl"
    journal = SessionJournal(journal_path, session_id="legacy-case")
    ledger = CaseWorkLedger(journal)
    turn = ledger.capture_turn("remember my preference", source="test", sequence=1)
    intent = ledger.create_intent(turn["turnId"], kind="foreground-dialogue", status="foreground")
    job = ledger.create_job(intent["intentId"], kind="dialogue", status="waiting-result")
    ledger.enqueue_result(intent["intentId"], {"message": "kept"}, job_id=job["jobId"])
    source_bytes = journal_path.read_bytes()

    database = ProductDatabase(tmp_path / "product.sqlite3")
    first = database.project_journal(journal, user_id="user-1")
    second = database.project_journal(journal, user_id="user-1")
    assert first == {"turns": 1, "intents": 1, "jobs": 1, "results": 1}
    assert second == first
    assert database.table_counts() == {"users": 1, "turns": 1, "intents": 1, "jobs": 1, "results": 1}
    assert journal_path.read_bytes() == source_bytes

    with database.session() as session:
        turn_row = session.get(ConversationTurnRow, turn["turnId"])
        assert turn_row is not None
        assert turn_row.correlation["turnId"] == turn["turnId"]
    assert ledger.journal.entries()[-1]["correlation"]["resultId"] == ledger.results()[next(iter(ledger.results()))]["correlation"]["resultId"]


def test_correlation_helpers_never_require_transcript_payloads():
    ids = correlation_ids(turn_id="turn-1", intent_id="intent-1", text="secret")
    assert ids == {"turn_id": "turn-1", "intent_id": "intent-1"}
    with product_span("test.span", {"turn_id": "turn-1", "text": "secret"}):
        pass


def test_incremental_projection_preserves_updates_and_rebuilds_after_restart(tmp_path):
    journal = SessionJournal(tmp_path / "session.jsonl", session_id="incremental")
    ledger = CaseWorkLedger(journal)
    turn = ledger.capture_turn("retained input", source="test")
    path = tmp_path / "product.sqlite3"
    database = ProductDatabase(path)
    original = database.project_journal(journal, user_id="user-1")
    ledger.set_turn_status(turn["turnId"], "processing")
    journal.append("runtime_event", {"eventType": "media.audio.batch"})
    assert database.project_journal(journal, user_id="user-1") == original
    with database.session() as session:
        assert session.get(ConversationTurnRow, turn["turnId"]).status == "processing"
    # A second projector instance must reconstruct counts without duplicating rows.
    database.close()
    restored = ProductDatabase(path)
    assert restored.project_journal(journal, user_id="user-1") == original
    assert restored.table_counts()["turns"] == 1
    with restored.session() as session:
        assert session.get(ConversationTurnRow, turn["turnId"]).status == "processing"
    restored.close()


def test_projection_does_not_advance_cursor_when_transaction_fails(tmp_path, monkeypatch):
    journal = SessionJournal(tmp_path / "session.jsonl", session_id="retry")
    ledger = CaseWorkLedger(journal)
    turn = ledger.capture_turn("retry input", source="test")
    database = ProductDatabase(tmp_path / "product.sqlite3")
    database.project_journal(journal, user_id="user-1")
    ledger.set_turn_status(turn["turnId"], "processing")
    project = database._project_entries
    def fail(*args):
        raise RuntimeError("disk unavailable")
    monkeypatch.setattr(database, "_project_entries", fail)
    with pytest.raises(RuntimeError, match="disk unavailable"):
        database.project_journal(journal, user_id="user-1")
    monkeypatch.setattr(database, "_project_entries", project)
    database.project_journal(journal, user_id="user-1")
    with database.session() as session:
        assert session.get(ConversationTurnRow, turn["turnId"]).status == "processing"
    database.close()


def test_local_artifact_port_scopes_catalog_and_rejects_path_escape(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    reference = root / "reference.pdf"
    reference.write_bytes(b"%PDF-test")
    port = LocalArtifactAdapter([{
        "id": "reference-1", "root": root, "path": "reference.pdf",
        "mediaType": "application/pdf", "skill": "assembly-coach",
        "tasks": ["task-1"], "steps": ["step-1"],
    }])

    visible = port.list_for_task(
        owner_id="user-1", skill="assembly-coach", task_id="task-1", step_id="step-1",
    )
    assert visible[0]["artifact_id"] == "reference-1"
    assert visible[0]["step_id"] == "step-1"
    assert visible[0]["checksum"].startswith("sha256:")
    assert port.list_for_task(
        owner_id="user-1", skill="other", task_id="task-1", step_id="step-1",
    ) == []

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-outside")
    with pytest.raises(ValueError, match="under its Skill"):
        LocalArtifactAdapter([{
            "id": "escaped", "root": root, "path": "../outside.pdf",
            "mediaType": "application/pdf",
        }])


def test_human_help_connector_preserves_idempotency_and_receipt(monkeypatch):
    captured = {}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def json(self, content_type=None):
            return {"status": "acknowledged", "requestId": "remote-1", "message": "queued"}

    class Session:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout.total

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def post(self, endpoint, *, json, headers):
            captured.update(endpoint=endpoint, json=json, headers=headers)
            return Response()

    request = HumanHelpRequest(
        request_id="help-1", user_id="user-1", intent_id="intent-1",
        connector="webhook", purpose="task assistance", preview={"message": "please help"},
        consent_id="consent-1", idempotency_key="help:help-1",
    )
    port = WebhookHumanHelpAdapter(
        "https://help.example.test/intake", token="secret", timeout_seconds=3,
        session_factory=Session,
    )
    receipt = asyncio.run(port.submit(request))

    assert receipt.status == "acknowledged"
    assert receipt.external_request_id == "remote-1"
    assert captured["headers"]["Idempotency-Key"] == "help:help-1"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert port.public_summary()["endpointOrigin"] == "https://help.example.test"
