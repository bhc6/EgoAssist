from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from egoassist.memory.timeline import TimelineMemory
from egoassist.product import (
    OutcomeLedger,
    OutcomeRecord,
    PrivacyGovernance,
    ProductDatabase,
    decrypt_json,
    encrypt_json,
    provider_disclosures,
    redact_pii,
    safe_metric_attributes,
)
from egoassist.product.models import ConversationTurnRow
from egoassist.realtime.identity import IdentityStore


def _governance(tmp_path: Path):
    identity = IdentityStore(tmp_path / "identity.sqlite3")
    user, _ = identity.register("privacy-user", "password123")
    database = ProductDatabase(tmp_path / "product.sqlite3")
    database.create_schema()
    database.ensure_user(user["id"], display_name="Privacy User")
    timeline = TimelineMemory(tmp_path / "timeline.sqlite3")
    ledger = OutcomeLedger(tmp_path / "outcomes.jsonl")
    ledger.record(OutcomeRecord("ack-1", "delivery", "dismissed", "dismissed", "user-acknowledgement", user_id=user["id"]))
    service = PrivacyGovernance(identity=identity, product_database=database, timeline=timeline, user_memory_root=tmp_path / "users", outcome_ledger=ledger)
    return user["id"], identity, database, timeline, service


def test_export_contains_identity_product_timeline_and_outcome(tmp_path):
    user_id, identity, database, timeline, service = _governance(tmp_path)
    identity.claim_context_session(user_id, "case-1", label="assembly")
    timeline.remember("prefer the left handle", user_id=user_id, case_id="case-1", scope="case", kind="fact", source_ids=["turn-1"])
    exported = service.export_account(user_id)
    assert exported["identity"]["contextSessions"][0]["id"] == "case-1"
    assert exported["timelineMemory"]["cards"][0]["caseId"] == "case-1"
    assert exported["evaluationOutcomes"][0]["source"] == "user-acknowledgement"
    assert exported["product"]["schema"] == "egoassist.account-export.v1"


def test_deletion_is_complete_idempotent_and_only_leaves_bounded_evidence(tmp_path):
    user_id, identity, database, timeline, service = _governance(tmp_path)
    timeline.remember("sensitive", user_id=user_id, case_id=None, scope="participant", kind="fact")
    evidence = service.delete_account(user_id)
    assert evidence["counts"]["memory"]["memoryCards"] == 1
    assert identity.export_user(user_id)["user"] is None
    assert service.delete_account(user_id)["counts"]["identity"]["users"] == 0
    evidence_files = list((tmp_path / "deletion-evidence").glob("*.json"))
    assert evidence_files
    assert "sensitive" not in evidence_files[0].read_text(encoding="utf-8")


def test_retention_deletes_expired_transcripts_and_reports_counts(tmp_path):
    user_id, identity, database, timeline, service = _governance(tmp_path)
    database.ensure_user(user_id)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    with database.session() as session:
        session.add(ConversationTurnRow(turn_id="old-turn", user_id=user_id, session_id="case", text="old", source="test", recorded_at=old, status="received", provenance={}, correlation={}))
    database.set_retention_policy(user_id, "transcript", 1)
    applied = service.retention(user_id=user_id, now=datetime.now(timezone.utc))
    assert applied["deleted"]["transcript"] == 1
    assert applied["evidence"] == "counts-only"


def test_memory_retention_uses_timeline_owner(tmp_path):
    user_id, identity, database, timeline, service = _governance(tmp_path)
    timeline.remember("old memory", user_id=user_id, case_id=None, scope="participant", kind="fact")
    database.set_retention_policy(user_id, "memory", 0)
    applied = service.retention(user_id=user_id, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert applied["deleted"]["memory"]["memoryCards"] == 1


def test_redaction_encryption_and_metric_boundary():
    value = redact_pii({"message": "姓名：张三，电话 13812345678，邮箱 a@example.com", "api_key": "sk-test-secret"})
    assert "张三" not in value["message"] and "13812345678" not in value["message"] and "a@example.com" not in value["message"]
    assert "secret" not in value["api_key"]
    labels = safe_metric_attributes({"turn_id": "turn-1", "transcript": "secret", "tool_arguments": "secret", "status": "ok"})
    assert labels == {"turn_id": "turn-1", "status": "ok"}
    key = Fernet.generate_key()
    payload = {"userId": "u1", "transcript": "private"}
    assert decrypt_json(encrypt_json(payload, key), key) == payload
    assert all(item["provider"] for item in provider_disclosures())
