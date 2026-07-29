from egoassist.realtime import IdentityStore


def test_identity_persists_login_and_session_ownership(tmp_path):
    path = tmp_path / "identity.sqlite3"
    store = IdentityStore(path)
    alice, alice_token = store.register("alice.test", "alice-password")
    bob, _ = store.register("bob.test", "bob-password")

    assert store.authenticate(alice_token) == alice
    assert store.login("ALICE.TEST", "alice-password")[0] == alice
    session_id = store.claim_context_session(
        alice["id"], "experiment-001", label="baseline interruption case",
    )
    assert session_id == "experiment-001"
    assert store.list_context_sessions(alice["id"])[0]["id"] == session_id
    assert store.get_context_session(alice["id"], session_id)["label"] == "baseline interruption case"
    assert store.list_context_sessions(bob["id"]) == []

    reopened = IdentityStore(path)
    assert reopened.authenticate(alice_token) == alice
    try:
        reopened.claim_context_session(bob["id"], session_id)
    except PermissionError:
        pass
    else:
        raise AssertionError("a context session must not be claimable by another user")

    reopened.logout(alice_token)
    assert reopened.authenticate(alice_token) is None


def test_identity_rejects_duplicate_user_and_bad_password(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    store.register("researcher", "valid-password")

    for operation in (
        lambda: store.register("RESEARCHER", "another-password"),
        lambda: store.login("researcher", "wrong-password"),
    ):
        try:
            operation()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid identity operation should fail")
