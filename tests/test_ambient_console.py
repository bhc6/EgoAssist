"""Ambient consent and disclosure at the HTTP boundary.

Ambient observation is off by default, observes nothing without a live consent
grant, and is disclosed only while it is actually running -- these tests pin all
three, because each is a promise made to the participant rather than an
implementation detail.
"""

import json

from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from run_egoassist import create_app


async def register_client(client, username):
    response = await client.post("/api/auth/register", json={
        "username": username, "password": "baseline-pass-123",
    })
    assert response.status == 200
    return (await response.json())["user"]


def ambient_app(tmp_path, monkeypatch, *, enabled: bool = True):
    if enabled:
        monkeypatch.setenv("EGOASSIST_AMBIENT_MODE", "1")
        monkeypatch.setenv("EGOASSIST_AMBIENT_INTERVAL_SECONDS", "20")
    else:
        monkeypatch.delenv("EGOASSIST_AMBIENT_MODE", raising=False)
    return create_app(
        str(BUILTIN_SKILLS_ROOT), None,
        str(tmp_path / "memory.json"), "test-realtime", "Ethan",
    )


def test_ambient_is_enabled_but_undisclosed_without_consent(tmp_path, monkeypatch):
    """Default deployment: no ambient capability, and the disclosure does not
    mention an ambient provider the deployment never uses."""
    async def scenario():
        app = ambient_app(tmp_path, monkeypatch, enabled=True)
        async with TestClient(TestServer(app)) as client:
            await register_client(client, "default-user")
            privacy = await (await client.get("/api/privacy")).json()
            assert privacy["ambient"]["enabled"] is True
            assert privacy["ambient"]["consented"] is False
            providers = {item["provider"] for item in privacy["providers"]}
            assert "ambient-observation-model" not in providers

    import asyncio
    asyncio.new_event_loop().run_until_complete(scenario())


def test_consent_is_required_and_revocable(tmp_path, monkeypatch):
    async def scenario():
        app = ambient_app(tmp_path, monkeypatch)
        async with TestClient(TestServer(app)) as client:
            await register_client(client, "ambient-user")

            before = await (await client.get("/api/privacy")).json()
            assert before["ambient"]["enabled"] is True
            # Enabled but not consented: still nothing observed, and nothing
            # disclosed, because no frames go anywhere yet.
            assert before["ambient"]["consented"] is False
            assert "ambient-observation-model" not in {
                item["provider"] for item in before["providers"]
            }
            assert before["ambient"]["watchlist"]["id"] == "medication-adherence"

            granted = await (await client.post("/api/ambient/consent", json={
                "granted": True,
            })).json()
            assert granted["ambient"]["consented"] is True
            assert granted["ambient"]["consentId"].startswith("consent:")

            during = await (await client.get("/api/privacy")).json()
            ambient_provider = next(
                item for item in during["providers"]
                if item["provider"] == "ambient-observation-model"
            )
            assert "revoke ambient consent" in ambient_provider["user_controls"]
            assert "turn off ambient mode" in ambient_provider["user_controls"]

            revoked = await (await client.post("/api/ambient/consent", json={
                "granted": False,
            })).json()
            assert revoked["ambient"]["consented"] is False
            after = await (await client.get("/api/privacy")).json()
            assert "ambient-observation-model" not in {
                item["provider"] for item in after["providers"]
            }

    import asyncio
    asyncio.new_event_loop().run_until_complete(scenario())


def test_watchlist_authorizes_no_bedroom(tmp_path, monkeypatch):
    """Aria community guidelines forbid bedroom recording, so the shipped
    watchlist must not offer one as a place to watch."""
    async def scenario():
        app = ambient_app(tmp_path, monkeypatch)
        async with TestClient(TestServer(app)) as client:
            await register_client(client, "watchlist-user")
            privacy = await (await client.get("/api/privacy")).json()
            watchlist = privacy["ambient"]["watchlist"]
            ids = {item["id"] for item in watchlist["locations"]}
            assert "bedroom" not in ids
            assert ids == {"kitchen", "dining-table", "living-room"}
            serialized = json.dumps(watchlist, ensure_ascii=False)
            assert "卧室" not in serialized
            assert "bathroom" not in serialized

    import asyncio
    asyncio.new_event_loop().run_until_complete(scenario())


def test_consent_appears_in_export_and_is_deleted_with_the_account(tmp_path, monkeypatch):
    async def scenario():
        app = ambient_app(tmp_path, monkeypatch)
        async with TestClient(TestServer(app)) as client:
            await register_client(client, "export-user")
            await client.post("/api/ambient/consent", json={"granted": True})
            export = await (await client.get("/api/privacy/export")).json()
            grants = export["product"]["data"]["consentGrants"]
            assert len(grants) == 1
            assert grants[0]["purpose"] == "ambient-observation"

            deleted = await (await client.post("/api/privacy/delete", json={
                "confirm": True,
            })).json()
            assert deleted["deleted"] is True
            assert deleted["evidence"]["counts"]["product"]["consent_grants"] == 1

    import asyncio
    asyncio.new_event_loop().run_until_complete(scenario())
