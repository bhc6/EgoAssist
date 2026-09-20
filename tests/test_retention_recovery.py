from datetime import datetime, timedelta, timezone

from test_privacy_governance import _governance

from egoassist.core.journal import SessionJournal
from egoassist.product.database import ProductDatabase
from egoassist.product.models import ConversationTurnRow
from egoassist.realtime.context_harness import ContextHarness
from egoassist.realtime.work_ledger import CaseWorkLedger


def test_expired_sources_and_derivatives_stay_erased_after_restart(tmp_path):
    user, identity, database, timeline, governance = _governance(tmp_path)
    identity.claim_context_session(user, "case")
    journal = SessionJournal(
        tmp_path / "users" / user / "sessions" / "case.jsonl", session_id="case"
    )
    observer = SessionJournal(journal.path)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    source = journal.append(
        "turn_inbox",
        {"turnId": "expired", "text": "private-old-secret", "timestamp": old},
    )
    journal.append_message(
        "user", "private-old-secret", source="test", metadata={"turnId": "expired"}
    )
    journal.append(
        "answer.job",
        {"turnId": "expired", "question": "private-old-secret", "status": "queued"},
    )
    journal.append(
        "compaction",
        {"summary": "private-old-secret", "compactedEntryIds": [source["id"]]},
    )
    recent = CaseWorkLedger(journal).capture_turn(
        "recent-safe-fact", source="test", turn_id="recent"
    )
    timeline.remember(
        "derived-private-old-secret",
        user_id=user,
        case_id="case",
        scope="case",
        kind="fact",
        source_ids=[source["id"]],
    )
    timeline.remember(
        "recent-safe-fact",
        user_id=user,
        case_id="case",
        scope="case",
        kind="fact",
        source_ids=[recent["id"]],
    )
    database.project_journal(journal, user_id=user)
    database.set_retention_policy(user, "transcript", 1)
    result = governance.retention(user_id=user)
    assert result["deleted"]["memory"] == {"memoryCards": 1, "memoryDrafts": 0}
    assert "private-old-secret" not in journal.path.read_text(encoding="utf-8")
    assert "private-old-secret" not in str(observer.entries())
    assert "private-old-secret" not in str(governance.export_account(user))
    assert "recent-safe-fact" in str(ContextHarness(observer).provider_replay_items())
    database.close()
    restarted = ProductDatabase(tmp_path / "product.sqlite3")
    restarted.project_journal(SessionJournal(journal.path), user_id=user)
    with restarted.session() as session:
        assert session.get(ConversationTurnRow, "expired") is None
        assert session.get(ConversationTurnRow, "recent") is not None
    restarted.close()


def test_projection_alone_cannot_resurrect_expired_turn(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    journal.append(
        "turn_inbox",
        {
            "turnId": "expired",
            "text": "old",
            "timestamp": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        },
    )
    database = ProductDatabase(tmp_path / "product.sqlite3")
    database.project_journal(journal, user_id="user")
    database.set_retention_policy("user", "transcript", 1)
    database.apply_retention(user_id="user")
    database.close()
    restarted = ProductDatabase(tmp_path / "product.sqlite3")
    restarted.project_journal(journal, user_id="user")
    with restarted.session() as session:
        assert session.get(ConversationTurnRow, "expired") is None
    restarted.close()


def test_observation_retention_removes_only_unreferenced_account_media(tmp_path):
    from egoassist.realtime.scene import SharedScene

    user, _, database, _, governance = _governance(tmp_path)
    journal = SessionJournal(
        tmp_path / "users" / user / "sessions" / "case.jsonl", session_id="case"
    )
    scene = SharedScene(journal, "")
    old_media = scene._save(b"old-media", ".jpg", "image/jpeg")
    recent_media = scene._save(b"recent-media", ".jpg", "image/jpeg")
    journal.append(
        "ambient.frame",
        {
            "frame": old_media,
            "timestamp": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        },
    )
    journal.append("ambient.frame", {"frame": recent_media})
    other = tmp_path / "users" / "other" / "sessions" / "media" / "case" / "other.jpg"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other-user")
    database.set_retention_policy(user, "observation", 1)
    governance.retention(user_id=user)
    assert not (scene.root / old_media["assetId"]).exists()
    assert (scene.root / recent_media["assetId"]).exists()
    assert other.read_bytes() == b"other-user"


def test_global_retention_also_applies_memory_policy(tmp_path):
    user, _, database, timeline, governance = _governance(tmp_path)
    timeline.remember(
        "old",
        user_id=user,
        case_id=None,
        scope="participant",
        kind="fact",
        recorded_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
    )
    database.set_retention_policy(user, "memory", 1)
    result = governance.retention()
    assert result["deleted"]["memory"] == {"memoryCards": 1, "memoryDrafts": 0}
    assert timeline.cards(user_id=user) == []
    repeated = governance.retention()
    assert repeated["deleted"]["memory"] == {"memoryCards": 0, "memoryDrafts": 0}


def test_erasing_derived_summary_does_not_erase_its_unexpired_sources(tmp_path):
    from egoassist.core.retention import erase_expired

    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    old = journal.append(
        "turn_inbox",
        {
            "turnId": "old",
            "text": "old-secret",
            "timestamp": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        },
    )
    recent = journal.append("turn_inbox", {"turnId": "recent", "text": "keep-this"})
    journal.append(
        "compaction",
        {
            "summary": "old-secret and keep-this",
            "compactedEntryIds": [old["id"], recent["id"]],
        },
    )
    erase_expired(
        journal, {"transcript": datetime.now(timezone.utc) - timedelta(days=1)}
    )
    remaining = journal.entries()
    assert "old-secret" not in str(remaining)
    assert "keep-this" in str(remaining)


def test_cross_case_derivatives_and_same_request_retry_cannot_restore_erased_text(
    tmp_path,
):
    user, _, database, timeline, governance = _governance(tmp_path)
    root = tmp_path / "users" / user / "sessions"
    first = SessionJournal(root / "z-source.jsonl", session_id="z-source")
    second = SessionJournal(root / "a-derived.jsonl", session_id="a-derived")
    old = first.append(
        "turn_inbox",
        {
            "turnId": "old",
            "text": "expired-shared-secret",
            "timestamp": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        },
        idempotency_key="request:old",
    )
    card = timeline.remember(
        "expired-shared-secret",
        user_id=user,
        case_id="z-source",
        scope="participant",
        kind="fact",
        source_ids=[old["id"]],
    )
    second.append(
        "answer.job",
        {
            "turnId": "new",
            "question": "ask again",
            "status": "completed",
            "text": "expired-shared-secret",
            "context": {"cardIds": [card["id"]]},
        },
    )
    second.append_message("user", "keep-unrelated", source="test")
    database.set_retention_policy(user, "transcript", 1)
    governance.retention(user_id=user)
    assert "expired-shared-secret" not in str(governance.export_account(user))
    assert "keep-unrelated" in str(second.entries())
    retry = first.append(
        "turn_inbox",
        {"turnId": "old", "text": "expired-shared-secret"},
        idempotency_key="request:old",
    )
    assert retry["type"] == "retention.deleted"
    assert "expired-shared-secret" not in first.path.read_text(encoding="utf-8")


def test_expired_memory_policy_also_erases_derived_answer_and_keeps_raw_recent_message(
    tmp_path,
):
    user, _, database, timeline, governance = _governance(tmp_path)
    journal = SessionJournal(
        tmp_path / "users" / user / "sessions" / "case.jsonl", session_id="case"
    )
    card = timeline.remember(
        "old-memory-value",
        user_id=user,
        case_id="case",
        scope="participant",
        kind="fact",
        recorded_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
    )
    journal.append(
        "answer.job",
        {
            "turnId": "new",
            "status": "completed",
            "text": "old-memory-value",
            "context": {"cardIds": [card["id"]]},
        },
    )
    journal.append_message("user", "recent-question", source="test")
    database.set_retention_policy(user, "memory", 1)
    governance.retention(user_id=user)
    assert "old-memory-value" not in str(governance.export_account(user))
    assert "recent-question" in str(journal.entries())
