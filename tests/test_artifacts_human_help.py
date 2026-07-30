import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from run_egoassist import create_app


async def register_client(client, username):
    response = await client.post("/api/auth/register", json={
        "username": username, "password": "baseline-pass-123",
    })
    assert response.status == 200
    return (await response.json())["user"]


def test_task_artifact_is_checkpoint_scoped_and_records_access(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client, "artifact-user")
            session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
            activated = await (await client.post("/api/skills/activate", json={
                "skill": "assembly-coach", "sessionId": session_id,
            })).json()
            assert activated["artifacts"] == []
            selected = await (await client.post("/api/tasks/select", json={
                "task": "water-filter", "sessionId": session_id,
            })).json()
            artifact = selected["artifacts"][0]
            assert artifact["artifact_id"] == "water-filter-reference"
            assert artifact["step_id"] == selected["task"]["currentCheckpointId"]
            assert artifact["previewUrl"].endswith("/content")

            content = await client.get(
                artifact["previewUrl"] + f"?session={session_id}"
            )
            assert content.status == 200
            assert content.headers["Content-Type"].startswith("application/pdf")
            assert (await content.read()).startswith(b"%PDF")

            tool = await client.post("/api/tools/execute", json={
                "sessionId": session_id, "tool": "show_reference",
                "arguments": {}, "confirmed": True,
            })
            assert tool.status == 200
            assert (await tool.json())["result"]["artifact"]["artifact_id"] == artifact["artifact_id"]

            flat_pack = await (await client.post("/api/tasks/select", json={
                "task": "flat-pack-stool", "sessionId": session_id,
            })).json()
            assert [item["artifact_id"] for item in flat_pack["artifacts"]] == [
                "flat-pack-stool-reference",
            ]
            stale_task_artifact = await client.get(
                artifact["previewUrl"] + f"?session={session_id}"
            )
            assert stale_task_artifact.status == 404

            await client.post("/api/task/reset", json={"sessionId": session_id})
            unauthorized = await client.get(
                flat_pack["artifacts"][0]["previewUrl"] + f"?session={session_id}"
            )
            assert unauthorized.status == 404

        journal = next((tmp_path / "users" / user["id"] / "sessions").glob("*.jsonl"))
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        receipt = next(item for item in entries if item.get("type") == "artifact_access_receipt")
        assert receipt["artifactId"] == "water-filter-reference"
        assert receipt["status"] == "resolved"
        assert receipt["checkpoint"]

    asyncio.run(scenario())


def test_human_help_requires_preview_and_surfaces_ack_failure_timeout_and_dedupe(
    tmp_path, monkeypatch,
):
    async def scenario():
        calls = []

        async def help_endpoint(request):
            body = await request.json()
            calls.append({
                "body": body,
                "idempotency": request.headers.get("Idempotency-Key"),
                "authorization": request.headers.get("Authorization"),
            })
            purpose = body["purpose"]
            if purpose == "fail":
                return web.json_response({"message": "unavailable"}, status=503)
            if purpose == "timeout":
                await asyncio.sleep(0.2)
                return web.json_response({"status": "acknowledged"})
            return web.json_response({
                "status": "acknowledged",
                "requestId": "remote-ack-1",
                "message": "helper accepted the request",
            })

        webhook_app = web.Application()
        webhook_app.router.add_post("/help", help_endpoint)
        webhook_server = TestServer(webhook_app)
        await webhook_server.start_server()
        try:
            monkeypatch.setenv(
                "EGOASSIST_HUMAN_HELP_WEBHOOK", str(webhook_server.make_url("/help")),
            )
            monkeypatch.setenv("EGOASSIST_HUMAN_HELP_TOKEN", "test-token")
            monkeypatch.setenv("EGOASSIST_HUMAN_HELP_TIMEOUT", "0.05")
            app = create_app(
                str(BUILTIN_SKILLS_ROOT), None,
                str(tmp_path / "memory.json"), "test-realtime", "Ethan",
            )
            async with TestClient(TestServer(app)) as client:
                await register_client(client, "human-help-user")
                session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
                await client.post("/api/skills/activate", json={
                    "skill": "kitchen-coach", "sessionId": session_id,
                })
                state = await (await client.get(f"/api/state?session={session_id}")).json()
                capability = next(
                    item for item in state["capabilityCatalog"]["platform"]
                    if item["id"] == "human-help"
                )
                assert capability["status"] == "available"
                assert state["humanHelp"]["endpointOrigin"].startswith("http://127.0.0.1:")

                async def submit(purpose):
                    preview_response = await client.post("/api/human-help/preview", json={
                        "sessionId": session_id,
                        "purpose": purpose,
                        "message": f"message for {purpose}",
                    })
                    assert preview_response.status == 200
                    preview = await preview_response.json()
                    assert preview["consentRequired"] is True
                    assert preview["preview"]["message"] == f"message for {purpose}"
                    assert len(calls) == {"ack": 0, "fail": 1, "timeout": 2}[purpose]
                    response = await client.post("/api/human-help/submit", json={
                        "sessionId": session_id,
                        "previewId": preview["previewId"],
                        "accepted": True,
                    })
                    assert response.status == 200, await response.text()
                    return preview, await response.json()

                ack_preview, ack = await submit("ack")
                assert ack["receipt"]["status"] == "acknowledged"
                assert ack["receipt"]["external_request_id"] == "remote-ack-1"
                assert calls[0]["authorization"] == "Bearer test-token"
                assert calls[0]["idempotency"].startswith("human-help:help-")
                duplicate = await (await client.post("/api/human-help/submit", json={
                    "sessionId": session_id,
                    "previewId": ack_preview["previewId"],
                    "accepted": True,
                })).json()
                assert duplicate["deduplicated"] is True
                assert len(calls) == 1

                _, failed = await submit("fail")
                assert failed["receipt"]["status"] == "failed"
                _, timed_out = await submit("timeout")
                assert timed_out["receipt"]["status"] == "timed-out"

                final_state = timed_out["state"]
                help_results = [
                    item for item in final_state["workLedger"]["resultInbox"]
                    if item["content"].get("kind") == "human-help"
                ]
                assert [item["content"]["status"] for item in help_results] == [
                    "acknowledged", "failed", "timed-out",
                ]
                jobs = [
                    item for item in final_state["workLedger"]["jobs"]
                    if item["kind"] == "human-help-submit"
                ]
                assert [item["status"] for item in jobs] == ["completed", "failed", "failed"]
        finally:
            await webhook_server.close()

    asyncio.run(scenario())
