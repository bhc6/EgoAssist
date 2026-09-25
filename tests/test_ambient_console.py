"""Ambient consent and disclosure at the HTTP boundary.

Ambient observation is enabled by default but observes nothing without live consent
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
            assert before["ambient"]["watchlist"]["id"] == "open-observation"

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


def test_default_observation_has_no_activity_or_location_whitelist(tmp_path, monkeypatch):
    async def scenario():
        app = ambient_app(tmp_path, monkeypatch)
        async with TestClient(TestServer(app)) as client:
            await register_client(client, "watchlist-user")
            privacy = await (await client.get("/api/privacy")).json()
            watchlist = privacy["ambient"]["watchlist"]
            assert watchlist["mode"] == "open"
            assert watchlist["locations"] == []
            assert watchlist["activities"] == []

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


def test_ambient_settings_defaults_and_revision_survive_repeated_control(tmp_path, monkeypatch):
    import asyncio
    async def scenario():
        app=ambient_app(tmp_path,monkeypatch)
        async with TestClient(TestServer(app)) as client:
            await register_client(client,'ambient-control')
            session=(await (await client.post('/api/sessions')).json())['session']['id']
            original=await (await client.get('/api/state',params={'session':session})).json()
            assert original['ambient']['questionsEnabled'] is False
            assert original['ambient']['remindersEnabled'] is True
            assert original['ambient']['reminderGoal'] == ''
            revisions=[];contexts=[]
            for _ in range(3):
                granted=await (await client.post('/api/ambient/consent',json={'granted':True,'sessionId':session})).json()
                assert granted['state']['ambient']['consented']
                contexts.append(granted['ambient']['binding']['contextId'])
                revisions.append(granted['ambient']['controlRevision'])
                settings=await (await client.post('/api/ambient/settings',json={'sessionId':session,
                    'intervalSeconds':7,'questionsEnabled':False,'questionCooldownSeconds':120})).json()
                assert settings['state']['ambient']['intervalSeconds']==7
                revisions.append(settings['ambient']['controlRevision'])
                stopped=await (await client.post('/api/ambient/consent',json={'granted':False,'sessionId':session})).json()
                assert not stopped['state']['ambient']['consented']
                revisions.append(stopped['ambient']['controlRevision'])
            assert revisions==sorted(set(revisions)) and len(set(contexts))==3
            state=await (await client.get('/api/state',params={'session':session})).json()
            assert state['ambient']['intervalSeconds']==7 and not state['ambient']['consented']
            invalid=await client.post('/api/ambient/settings',json={'sessionId':session,'intervalSeconds':0,'questionsEnabled':False})
            assert invalid.status==400
            for field, value in [('remindersEnabled', 'false'), ('reminderGoal', 'x'*601)]:
                invalid = await client.post('/api/ambient/settings', json={'sessionId': session,
                    'intervalSeconds': 7, 'questionsEnabled': False, field: value})
                assert invalid.status == 400
            changed = await (await client.post('/api/ambient/settings', json={'sessionId': session,
                'intervalSeconds': 7, 'questionsEnabled': False, 'remindersEnabled': False,
                'reminderGoal': '  看到钥匙提醒我收好  '})).json()
            assert changed['ambient']['reminderGoal'] == '看到钥匙提醒我收好'
            assert not changed['ambient']['remindersEnabled']
            compatible = await (await client.post('/api/ambient/settings', json={'sessionId': session,
                'intervalSeconds': 8, 'questionsEnabled': False})).json()
            assert compatible['ambient']['reminderGoal'] == '看到钥匙提醒我收好'
            assert not compatible['ambient']['remindersEnabled']
    asyncio.run(scenario())
