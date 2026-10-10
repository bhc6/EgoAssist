import asyncio

from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.memory import TimelineMemory
from run_egoassist import create_app
from tests.support import register_client


def test_pending_claim_api_lists_confirms_rejects_and_enforces_owner(tmp_path):
    async def scenario():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "memory.json"), "test-realtime", "Ethan")
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client, "claims-owner")
            session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
            timeline = TimelineMemory(tmp_path / "timeline.sqlite3")
            keep = timeline.remember("用户把护照放进了蓝色背包", user_id=user["id"], case_id=session_id,
                                     scope="case", kind="event", status="pending")
            drop = timeline.remember("用户把雨伞放在门口", user_id=user["id"], case_id=session_id,
                                     scope="case", kind="event", status="pending")

            listed = await (await client.get("/api/memory/pending", params={"session": session_id})).json()
            assert {claim["id"] for claim in listed["claims"]} == {keep["id"], drop["id"]}

            confirmed = await client.post("/api/memory/pending", json={
                "sessionId": session_id, "cardId": keep["id"], "accepted": True})
            assert confirmed.status == 200 and (await confirmed.json())["card"]["status"] == "active"
            rejected = await client.post("/api/memory/pending", json={
                "sessionId": session_id, "cardId": drop["id"], "accepted": False})
            assert (await rejected.json())["card"]["status"] == "deleted"
            again = await client.post("/api/memory/pending", json={
                "sessionId": session_id, "cardId": keep["id"], "accepted": True})
            assert again.status == 404
            missing = await client.post("/api/memory/pending", json={"sessionId": session_id})
            assert missing.status == 400

            other = timeline.remember("别人的待确认记忆", user_id="someone-else", case_id="x",
                                      scope="case", kind="event", status="pending")
            stolen = await client.post("/api/memory/pending", json={"cardId": other["id"], "accepted": True})
            assert stolen.status == 404
            assert timeline.cards_by_ids([other["id"]], user_id="someone-else")[other["id"]]["status"] == "pending"
    asyncio.run(scenario())
