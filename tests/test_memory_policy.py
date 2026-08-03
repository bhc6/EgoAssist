import pytest

from egoassist.realtime import (
    IdentityStore, default_memory_policy, memory_profiles, normalize_memory_policy,
)


def test_memory_profiles_are_explicit_and_default_all_online():
    policy = default_memory_policy()

    assert policy["profile"] == "all-online"
    assert policy["components"] == {
        "timeline": {
            "enabled": True,
            "preference_enabled": True,
            "episodic_enabled": True,
            "scope": "case",
        },
    }
    assert {item["id"] for item in memory_profiles()} == {
        "all-online", "context-only", "confirmed-preference", "lexical-episodic",
    }
    assert policy["infrastructure"]["contextJournal"] == "required"


def test_memory_policy_normalizes_custom_ablation_and_rejects_unknown_values():
    custom = normalize_memory_policy({
        "profile": "custom",
        "components": {
            "timeline": {
                "enabled": True,
                "preference_enabled": False,
                "episodic_enabled": True,
                "scope": "participant",
            },
        },
    })

    assert custom["profile"] == "custom"
    assert custom["components"]["timeline"] == {
        "enabled": True,
        "preference_enabled": False,
        "episodic_enabled": True,
        "scope": "participant",
    }
    with pytest.raises(ValueError, match="unknown memory profile"):
        normalize_memory_policy({"profile": "opaque-retriever"})
    with pytest.raises(ValueError, match="must be case or participant"):
        normalize_memory_policy({
            "profile": "custom",
            "components": {"timeline": {"scope": "global"}},
        })


def test_case_memory_policy_persists_in_sqlite(tmp_path):
    path = tmp_path / "identity.sqlite3"
    store = IdentityStore(path)
    user, _ = store.register("policy-user", "baseline-pass-123")
    session_id = store.claim_context_session(user["id"], label="B0")

    assert store.get_context_session(user["id"], session_id)["memoryPolicy"]["profile"] == "all-online"
    stored = store.update_memory_policy(user["id"], session_id, {"profile": "context-only"})
    assert stored["profile"] == "context-only"

    reopened = IdentityStore(path)
    restored = reopened.get_context_session(user["id"], session_id)["memoryPolicy"]
    assert restored == stored
    assert restored["version"] == 2
