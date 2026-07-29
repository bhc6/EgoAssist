import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, CookieJar
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.client_exceptions import WSServerHandshakeError

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.memory import TimelineMemory
from egoassist.realtime import PreferenceMemory
import run_egoassist
from run_egoassist import create_app


ROOT = Path(__file__).parents[1]


def test_runtime_root_lease_is_exclusive_and_reusable(tmp_path):
    first = run_egoassist.RuntimeRootLease(tmp_path / "memory.json")
    second = run_egoassist.RuntimeRootLease(tmp_path / "memory.json")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already owned"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def patch_semantic_router(monkeypatch):
    async def classify(_self, text, catalog):
        stop = text in {"您别说了。", "请停止当前语音输出"}
        selected_skill = (
            "kitchen-coach" if text == "cook fruit salad"
            else "assembly-coach" if text == "assemble flat pack stool"
            else None
        )
        deactivate = text == "exit current skill"
        return {
            "method": "semantic-turn-router-v3",
            "model": "test-router",
            "status": "classified",
            "speechControl": "stop-speaking" if stop else None,
            "taskControl": "repeat" if text == "再说一遍" else None,
            "preferenceIntent": None,
            "memoryQuery": None,
            "skillControl": "deactivate" if deactivate else None,
            "selectedSkill": selected_skill,
            "skillStatus": "matched" if selected_skill else "no-match",
            "rawOutput": json.dumps({
                "speechControl": "stop-speaking" if stop else "none",
                "taskControl": "repeat" if text == "再说一遍" else "none",
                "preferenceIntent": "none", "memoryQuery": "none",
                "skillControl": "deactivate" if deactivate else "none",
                "skill": selected_skill or "none",
            }),
            "latencyMs": 0.1,
            "policyVersion": "semantic-turn-router-v3",
            "claimBoundary": "test classifier",
        }

    monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)


async def register_client(client, username="researcher"):
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "password": "baseline-pass-123"},
    )
    assert response.status == 200
    return (await response.json())["user"]


def semantic_result(text, *, selected_skill=None):
    return {
        "method": "semantic-turn-router-v3",
        "model": "test-router",
        "status": "classified",
        "speechControl": None,
        "taskControl": None,
        "preferenceIntent": None,
        "memoryQuery": None,
        "skillControl": None,
        "selectedSkill": selected_skill,
        "skillStatus": "matched" if selected_skill else "no-match",
        "rawOutput": json.dumps({"text": text}),
        "latencyMs": 0.1,
        "policyVersion": "semantic-turn-router-v3",
        "claimBoundary": "test classifier",
    }


def install_queue_test_bridge(monkeypatch):
    instances = []

    class FakeBridge:
        def __init__(self, api_key, model, voice, instructions, emit, on_transcript):
            self.emit = emit
            self.on_transcript = on_transcript
            self.requests = []
            self.injections = []
            self.cancellations = []
            instances.append(self)

        async def connect(self):
            await self.emit({"type": "ready"})

        def set_context_harness(self, harness):
            self.harness = harness

        def set_tools(self, declarations, handler):
            self.declarations = declarations
            self.tool_handler = handler

        def set_trace_sink(self, sink):
            self.trace_sink = sink

        def update_context(self, instructions):
            self.instructions = instructions

        def inject_context_data(self, label, value):
            self.injections.append((label, value))
            return True

        def request_response(self, instructions, priority=50, interrupt=False):
            self.requests.append((instructions, priority, interrupt))

        def append_audio(self, value):
            return False

        def append_video(self, value):
            return False

        def maybe_check_proactively(self, interval):
            return False

        def cancel_response(self, **kwargs):
            self.cancellations.append(kwargs)
            return True

        async def close(self):
            return None

    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setattr("egoassist.realtime.omni.OmniBrowserBridge", FakeBridge)
    return instances


def test_grounded_turn_claims_follow_committed_memory_state(tmp_path, monkeypatch):
    instances = install_queue_test_bridge(monkeypatch)

    async def classify(_self, text, catalog):
        decision = semantic_result(text)
        decision.update({
            "method": "semantic-turn-router-v4",
            "policyVersion": "semantic-turn-router-v4",
            "memoryKind": None,
            "memoryText": None,
            "memoryKey": None,
            "memoryScope": None,
            "memoryConsent": None,
        })
        if text == "记住一个号码":
            decision.update({
                "memoryKind": "capture", "memoryScope": "participant",
                "memoryConsent": "explicit",
            })
        elif text == "我的号码是13657352908":
            decision.update({
                "memoryKind": "fact", "memoryText": "用户的电话号码是13657352908",
                "memoryKey": "contact:user:phone", "memoryScope": "participant",
                "memoryConsent": "explicit",
            })
        return decision

    monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client, "claim-boundary")
            session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
            ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await ws.receive_json())["type"] == "ready"
            assert (await ws.receive_json())["type"] == "state"

            async def control(text):
                await ws.send_json({"type": "control", "transcript": text})
                messages = [await ws.receive_json() for _ in range(3)]
                assert [item["type"] for item in messages] == ["user_transcript", "state", "interrupted"]
                return instances[0].requests[-1][0]

            capture_instruction = await control("记住一个号码")
            assert "No memory value was committed" in capture_instruction
            assert "never say it is saved" in capture_instruction

            committed_instruction = await control("我的号码是13657352908")
            assert "memory card was durably committed" in committed_instruction
            assert "do not claim that any reminder" in committed_instruction

            ordinary_instruction = await control("你现在记住了吗")
            assert "No active memory card was committed" in ordinary_instruction
            assert "No tool result has been committed" in ordinary_instruction
            assert instances[0].injections[-1] == (
                "current_turn_context",
                {"transcript": "你现在记住了吗", "retrievalEvidence": []},
            )
            await ws.close()

    asyncio.run(scenario())


def test_participant_memory_survives_restart_when_case_timeline_is_disabled(tmp_path):
    memory_path = tmp_path / "memory.json"
    username = "restart-memory"
    password = "baseline-pass-123"
    session_id = None
    user_id = None

    async def first_process():
        nonlocal session_id, user_id
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(memory_path), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client, username)
            user_id = user["id"]
            session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
            configured = await client.post("/api/sessions/memory-policy", json={
                "sessionId": session_id,
                "policy": {"profile": "confirmed-preference"},
            })
            assert configured.status == 200
            assert (await configured.json())["memoryPolicy"]["components"]["episodic"]["enabled"] is False
            await client.post("/api/voice", json={
                "sessionId": session_id,
                "transcript": "请记住小李的电话号码",
                "memoryKind": "capture", "memoryScope": "participant", "memoryConsent": "explicit",
            })
            written = await client.post("/api/voice", json={
                "sessionId": session_id,
                "transcript": "13657352908",
                "memoryKind": "fact", "memoryText": "小李的电话号码是13657352908",
                "memoryKey": "contact:小李:phone", "memoryScope": "case", "memoryConsent": "observed",
            })
            assert written.status == 200

    async def second_process():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(memory_path), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            login = await client.post("/api/auth/login", json={"username": username, "password": password})
            assert login.status == 200
            recalled = await client.post("/api/voice", json={
                "sessionId": session_id,
                "transcript": "把小李的电话再告诉我一次",
                "memoryQuery": "referential",
            })
            assert recalled.status == 200
            recalled_state = await recalled.json()
            card_id = next(
                card["id"] for card in recalled_state["episodicMemory"]["timelineViews"]["participant"]["recent"]
                if card["text"] == "小李的电话号码是13657352908"
            )
            deleted = await client.post("/api/memory/delete", json={
                "sessionId": session_id, "id": card_id,
            })
            assert deleted.status == 200

    asyncio.run(first_process())
    asyncio.run(second_process())

    journal_path = tmp_path / "users" / user_id / "sessions" / f"{session_id}.jsonl"
    entries = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    latest_context = next(
        entry for entry in reversed(entries)
        if entry.get("type") == "turn_context" and entry.get("transcript") == "把小李的电话再告诉我一次"
    )
    assert any(
        item.get("summary") == "小李的电话号码是13657352908"
        for item in latest_context["retrievalEvidence"]
    )
    cards = TimelineMemory(tmp_path / "timeline.sqlite3").cards(
        user_id=user_id, scope="participant", statuses=("active", "deleted"),
    )
    assert [(card["text"], card["status"]) for card in cards] == [
        ("小李的电话号码是13657352908", "deleted"),
    ]


def test_explicit_general_preference_is_active_without_second_confirmation(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client, "explicit-preference")
            session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
            response = await client.post("/api/voice", json={
                "sessionId": session_id,
                "transcript": "以后回答都简短一点",
                "preferenceIntent": "propose-durable",
                "memoryKind": "preference",
                "memoryText": "用户偏好简短回答",
                "memoryKey": "preference:response-style",
                "memoryScope": "participant",
                "memoryConsent": "explicit",
            })
            assert response.status == 200
            state = await response.json()
            assert state["memory"]["candidate"] is None
            assert any(
                record["preference"] == "用户偏好简短回答" and record["status"] == "confirmed"
                for record in state["memory"]["records"]
            )

    asyncio.run(scenario())


def test_runtime_root_prefers_caller_env_and_falls_back_to_source(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    working_root = tmp_path / "working"
    source_root.mkdir()
    working_root.mkdir()
    (source_root / ".env").write_text("QWEN_API_KEY=test", encoding="utf-8")
    monkeypatch.setattr(run_egoassist, "ROOT", source_root)
    monkeypatch.chdir(working_root)

    assert run_egoassist.runtime_root() == source_root
    (working_root / ".env").write_text("QWEN_API_KEY=override", encoding="utf-8")
    assert run_egoassist.runtime_root() == working_root


def test_research_voice_api_rejects_unknown_semantic_labels(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            response = await client.post("/api/voice", json={
                "transcript": "evidence", "taskControl": "invented-action",
            })
            assert response.status == 400
            assert "invalid taskControl" in (await response.json())["error"]

    asyncio.run(scenario())


def test_api_switches_catalog_tasks_and_isolates_custom_tasks(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            initial = await (await client.get("/api/state")).json()
            assert {item["id"] for item in initial["tasks"]} == {"tomato-eggs", "oatmeal", "fruit-salad"}
            assert initial["memoryMechanism"]["policy"] == "explicit-confirmation"
            assert initial["memoryMechanism"]["evidence"]["status"] == "waiting"
            assert initial["memoryMechanism"]["retrieval"]["status"] == "no-match"

            selected = await (await client.post("/api/tasks/select", json={"task": "oatmeal"})).json()
            assert selected["task"]["task"]["title"] == "香蕉燕麦粥"
            assert "番茄" not in "".join(item["instruction"] for item in selected["task"]["steps"])

            replay = await (await client.post("/api/replay/observe", json={"kind": "wrong_step"})).json()
            assert replay["task"]["decision"]["action"] == "ask"
            assert replay["task"]["observation"]["source"] == "research-replay/deterministic"

            custom = await (await client.post("/api/tasks/create", json={
                "title": "整理书桌", "steps": ["移走无关物品", "分类文件", "擦拭桌面"],
            })).json()
            custom_id = custom["task"]["task"]["id"]
            assert custom["task"]["task"]["provenance"] == "user-defined"

            assembly = await (await client.post("/api/skills/activate", json={"skill": "assembly-coach"})).json()
            assert custom_id not in {item["id"] for item in assembly["tasks"]}
            rejected = await client.post("/api/tasks/select", json={"task": "oatmeal"})
            assert rejected.status == 400
            assert "unknown task" in (await rejected.json())["error"]

    asyncio.run(scenario())


def test_generic_start_routes_skill_then_task_without_default_recipe(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            initial = await (await client.get("/api/state")).json()
            assert initial["activeSkill"] is None
            assert initial["task"] is None
            assert initial["intentRouting"]["status"] == "no-match"
            assert initial["knowledgeMechanism"]["method"] == "none"

            skill_only = await (await client.post(
                "/api/voice", json={"transcript": "我想做菜，但还没想好做什么"},
            )).json()
            assert skill_only["activeSkill"]["name"] == "kitchen-coach"
            assert skill_only["task"] is None
            assert skill_only["intentRouting"]["selectedTask"] is None

            selected = await (await client.post(
                "/api/voice", json={"transcript": "那就做水果沙拉"},
            )).json()
            assert selected["task"]["task"]["id"] == "fruit-salad"
            assert selected["intentRouting"]["selectedTask"] == "fruit-salad"
            assert selected["episodicMemory"]["count"] == 0
            assert selected["episodicMemory"]["retainsRawMedia"] is False

            reset = await (await client.post("/api/task/reset", json={})).json()
            assert reset["activeSkill"] is None
            assert reset["task"] is None
            assert reset["intentRouting"]["status"] == "no-match"
            assert reset["intentRouting"]["selectedSkill"] is None

    asyncio.run(scenario())


def test_case_scoped_skill_lifecycle_is_durable_and_isolated(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client)
            first = (await (await client.post(
                "/api/sessions", json={"label": "first"},
            )).json())["session"]["id"]
            second = (await (await client.post(
                "/api/sessions", json={"label": "second"},
            )).json())["session"]["id"]

            activated = await (await client.post("/api/skills/activate", json={
                "sessionId": first, "skill": "kitchen-coach",
            })).json()
            assert activated["sessionId"] == first
            assert activated["activeSkill"]["name"] == "kitchen-coach"

            selected = await (await client.post("/api/tasks/select", json={
                "sessionId": first, "task": "fruit-salad",
            })).json()
            assert selected["task"]["task"]["id"] == "fruit-salad"
            isolated = await (await client.get(f"/api/state?session={second}")).json()
            assert isolated["activeSkill"] is None

            deactivated = await (await client.post("/api/skills/activate", json={
                "sessionId": first, "skill": "",
            })).json()
            assert deactivated["activeSkill"] is None
            assert deactivated["task"] is None

            restored = await (await client.get(f"/api/state?session={first}")).json()
            assert restored["activeSkill"] is None
            assert restored["task"] is None

        journal_path = tmp_path / "users" / user["id"] / "sessions" / f"{first}.jsonl"
        entries = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
        transitions = [
            item["eventType"] for item in entries
            if item.get("type") == "runtime_event"
            and str(item.get("eventType", "")).startswith("runtime.skill.")
        ]
        assert "runtime.skill.activated" in transitions
        assert "runtime.skill.deactivated" in transitions
        checkpoints = [item for item in entries if item.get("type") == "state_checkpoint"]
        assert checkpoints[-1]["state"]["skill"] is None
        assert checkpoints[-1]["state"]["task"] is None

    asyncio.run(scenario())


def test_manifest_evidence_switches_active_skill_and_task_but_abstains_without_evidence(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "assembly-coach",
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            await client.post("/api/tasks/select", json={"task": "flat-pack-stool"})

            switched_skill = await (await client.post(
                "/api/voice", json={"transcript": "我想做菜，但还没想好做什么"},
            )).json()
            assert switched_skill["activeSkill"]["name"] == "kitchen-coach"
            assert switched_skill["task"] is None

            fruit = await (await client.post(
                "/api/voice", json={"transcript": "请指导我做水果沙拉"},
            )).json()
            assert fruit["task"]["task"]["id"] == "fruit-salad"

            tomato = await (await client.post(
                "/api/voice", json={"transcript": "番茄炒蛋"},
            )).json()
            assert tomato["task"]["task"]["id"] == "tomato-eggs"

            abstained = await (await client.post(
                "/api/voice", json={"transcript": "今天天气还不错"},
            )).json()
            assert abstained["task"]["task"]["id"] == "tomato-eggs"
            assert abstained["intentRouting"]["status"] == "no-match"

    asyncio.run(scenario())


def test_authenticated_users_have_isolated_runtime_preferences_episodes_and_session_lists(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        server = TestServer(app)
        await server.start_server()
        try:
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as alice_client, \
                    ClientSession(cookie_jar=CookieJar(unsafe=True)) as bob_client:
                base = str(server.make_url("/"))[:-1]

                anonymous = await alice_client.get(base + "/api/state")
                assert anonymous.status == 401
                alice = (await (await alice_client.post(
                    base + "/api/auth/register",
                    json={"username": "alice", "password": "alice-password"},
                )).json())["user"]
                bob = (await (await bob_client.post(
                    base + "/api/auth/register",
                    json={"username": "bob-user", "password": "bob-password"},
                )).json())["user"]
                assert alice["id"] != bob["id"]

                alice_state = await (await alice_client.post(
                    base + "/api/voice", json={"transcript": "请指导我做水果沙拉"},
                )).json()
                assert alice_state["activeSkill"]["name"] == "kitchen-coach"
                await alice_client.post(
                    base + "/api/voice", json={
                        "transcript": "以后每次到这里请提醒我慢一点",
                        "preferenceIntent": "propose-durable",
                    },
                )
                await alice_client.post(base + "/api/candidate", json={"accepted": True})
                await alice_client.post(base + "/api/voice", json={
                    "transcript": "记住一段数字",
                    "memoryKind": "capture", "memoryScope": "participant", "memoryConsent": "explicit",
                })
                await alice_client.post(base + "/api/voice", json={
                    "transcript": "四五六七八九二",
                    "memoryKind": "fact", "memoryText": "用户要求记住的号码是四五六七八九二",
                    "memoryKey": "remembered-number", "memoryScope": "case", "memoryConsent": "observed",
                })

                bob_state = await (await bob_client.get(base + "/api/state")).json()
                assert bob_state["activeSkill"] is None
                assert bob_state["memory"]["records"] == []
                assert alice_state["episodicMemory"]["path"] == bob_state["episodicMemory"]["path"]
                assert Path(alice_state["episodicMemory"]["path"]).name == "timeline.sqlite3"
                bob_recall = await (await bob_client.post(
                    base + "/api/voice", json={"transcript": "我刚才让你记住的数字是什么来着"},
                )).json()
                evidence = bob_recall["episodicMemory"]["retrieval"]["injection"].get("evidence", [])
                assert all(item.get("summary") != "四五六七八九二" for item in evidence)

                alice_session = (await (await alice_client.post(base + "/api/sessions")).json())["session"]["id"]
                reset_for_case = await (await alice_client.get(base + "/api/state")).json()
                assert reset_for_case["activeSkill"] is None
                assert reset_for_case["task"] is None
                bob_session = (await (await bob_client.post(base + "/api/sessions")).json())["session"]["id"]
                alice_sessions = (await (await alice_client.get(base + "/api/sessions")).json())["sessions"]
                bob_sessions = (await (await bob_client.get(base + "/api/sessions")).json())["sessions"]
                assert {item["id"] for item in alice_sessions} == {alice_session}
                assert {item["id"] for item in bob_sessions} == {bob_session}

                for path, body in (
                    ("/api/sessions/memory-policy", {"sessionId": alice_session, "policy": {"profile": "context-only"}}),
                    ("/api/candidate", {"sessionId": alice_session, "accepted": True}),
                    ("/api/memory/delete", {"sessionId": alice_session, "id": "unknown"}),
                ):
                    forbidden = await bob_client.post(base + path, json=body)
                    assert forbidden.status == 403
                forbidden_state = await bob_client.get(base + f"/api/state?session={alice_session}")
                assert forbidden_state.status == 403

                alice_case_policy = await alice_client.post(
                    base + "/api/sessions/memory-policy",
                    json={
                        "sessionId": alice_session,
                        "policy": {
                            "profile": "custom",
                            "components": {
                                "preference": {"enabled": True, "scope": "case"},
                                "episodic": {"enabled": True, "scope": "case"},
                            },
                        },
                    },
                )
                assert alice_case_policy.status == 200
                scoped_state = await (await alice_client.get(
                    base + f"/api/state?session={alice_session}",
                )).json()
                assert scoped_state["sessionId"] == alice_session
                assert Path(scoped_state["memory"]["memoryPath"]) == tmp_path / "timeline.sqlite3"
                assert Path(scoped_state["episodicMemory"]["path"]) == tmp_path / "timeline.sqlite3"
        finally:
            await server.close()

    asyncio.run(scenario())


def test_generic_episodic_recall_is_not_elevated_into_system_context(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            await client.post("/api/voice", json={
                "transcript": "记住一段数字",
                "memoryKind": "capture", "memoryScope": "participant", "memoryConsent": "explicit",
            })
            await client.post("/api/voice", json={
                "transcript": "四五六七八九二",
                "memoryKind": "fact", "memoryText": "用户要求记住的号码是四五六七八九二",
                "memoryKey": "remembered-number", "memoryScope": "case", "memoryConsent": "observed",
            })
            recalled = await (await client.post(
                "/api/voice", json={
                    "transcript": "我刚才让你记住的数字是什么来着",
                    "memoryQuery": "referential",
                },
            )).json()

            injection = recalled["episodicMemory"]["retrieval"]["injection"]
            assert injection["status"] == "injected"
            assert any(item["summary"] == "用户要求记住的号码是四五六七八九二" for item in injection["evidence"])
            assert "四五六七八九二" not in recalled["promptContext"]["text"]
            assert "episode=" not in recalled["promptContext"]["text"]

    asyncio.run(scenario())


def test_voice_repeat_is_json_and_only_explicit_durable_preferences_create_memory(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            repeated_response = await client.post("/api/voice", json={
                "transcript": "再说一遍", "taskControl": "repeat",
            })
            assert repeated_response.status == 200
            repeated = await repeated_response.json()
            assert repeated["task"]["decision"]["reason"] == "用户请求重复"
            assert repeated["memory"]["candidate"] is None

            preference = await (await client.post(
                "/api/voice", json={
                    "transcript": "以后每次到这里请提醒我慢一点",
                    "preferenceIntent": "propose-durable",
                },
            )).json()
            assert preference["memory"]["candidate"]["status"] == "pending"
            assert preference["memory"]["candidate"]["prompt"] == "以后每次到这里请提醒我慢一点"
            assert preference["memory"]["candidate"]["sourceQuote"] == "以后每次到这里请提醒我慢一点"
            assert preference["memory"]["candidate"]["trigger"]["task"] == preference["task"]["task"]["id"]
            assert preference["memoryMechanism"]["candidate"]["status"] == "pending"

            recalled = await (await client.post("/api/candidate", json={"accepted": True})).json()
            assert recalled["memoryMechanism"]["confirmation"]["count"] == 1
            assert recalled["memoryMechanism"]["retrieval"]["status"] == "matched"
            assert recalled["memoryMechanism"]["impact"]["status"] == "ready"

    asyncio.run(scenario())


def test_quick_control_uses_the_realtime_websocket(tmp_path, monkeypatch):
    instances = []

    class FakeBridge:
        def __init__(self, api_key, model, voice, instructions, emit, on_transcript):
            self.emit = emit
            self.on_transcript = on_transcript
            self.requests = []
            self.cancellations = []
            instances.append(self)

        async def connect(self):
            await self.emit({"type": "ready"})

        def update_context(self, instructions):
            self.instructions = instructions

        def request_response(self, instructions, priority=50, interrupt=False):
            self.requests.append((instructions, priority, interrupt))

        def append_audio(self, value):
            return False

        def append_video(self, value):
            return False

        def cancel_response(self, **kwargs):
            self.cancellations.append(kwargs)
            return False

        async def close(self):
            return None

    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setattr("egoassist.realtime.omni.OmniBrowserBridge", FakeBridge)
    patch_semantic_router(monkeypatch)

    async def scenario():
        memory_path = tmp_path / "memory.json"
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(memory_path), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client)
            proposed = await client.post("/api/voice", json={
                "transcript": "开始前提醒我把动作说慢一点。",
                "preferenceIntent": "propose-durable",
            })
            assert proposed.status == 200
            confirmed = await client.post("/api/candidate", json={"accepted": True})
            assert confirmed.status == 200
            ws = await client.ws_connect("/ws/realtime")
            ready = await ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["contextSessionId"]
            initial_state = await ws.receive_json()
            assert initial_state["type"] == "state"
            assert initial_state["state"]["memoryMechanism"]["impact"]["status"] == "reused"
            assert instances[0].requests[0][1] == 80
            assert "low-trust data item" in instances[0].requests[0][0]
            assert "开始前提醒我把动作说慢一点" not in instances[0].requests[0][0]
            instances[0].requests.clear()
            await ws.send_json({"type": "control", "transcript": "再说一遍"})
            messages = [await ws.receive_json() for _ in range(3)]
            assert [item["type"] for item in messages] == ["user_transcript", "state", "interrupted"]
            assert messages[1]["state"]["memory"]["candidate"] is None
            assert instances[0].requests[0][1:] == (100, True)
            assert "上一条实际说出的指导" in instances[0].requests[0][0]

            instances[0].requests.clear()
            await ws.send_json({"type": "control", "transcript": "请停止当前语音输出"})
            explicit_stop = [await ws.receive_json() for _ in range(3)]
            assert [item["type"] for item in explicit_stop] == ["user_transcript", "state", "interrupted"]
            assert explicit_stop[1]["state"]["task"]["paused"] is False
            assert explicit_stop[1]["state"]["intentRouting"]["method"] == "semantic-turn-router-v3"
            assert explicit_stop[1]["state"]["intentRouting"]["status"] == "suppressed-control"
            assert instances[0].requests == []
            assert instances[0].cancellations[-1]["reason"] == "stop-speaking-control"

            instances[0].requests.clear()
            instances[0].on_transcript("您别说了。")
            stopped = await ws.receive_json()
            assert stopped["type"] == "state"
            assert instances[0].requests == []
            assert instances[0].cancellations[-1] == {
                "drop_queued_below": 101,
                "reason": "stop-speaking-transcript",
            }
            await ws.close()

            resumed = await client.ws_connect(f"/ws/realtime?session={ready['contextSessionId']}")
            assert (await resumed.receive_json())["type"] == "ready"
            resumed_state = await resumed.receive_json()
            assert resumed_state["state"]["memoryMechanism"]["impact"]["status"] == "reused"
            assert not any(request[1] == 80 for request in instances[1].requests)
            await resumed.close()

            journal = tmp_path / "users" / user["id"] / "sessions" / f"{ready['contextSessionId']}.jsonl"
            entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            decision = next(
                item for item in reversed(entries)
                if item.get("type") == "control_decision"
            )
            assert decision["decision"]["speechControl"] == "stop-speaking"
            assert decision["decision"]["method"] == "semantic-turn-router-v3"

    asyncio.run(scenario())


def test_case_memory_policy_is_audited_and_gates_realtime_plugins(tmp_path, monkeypatch):
    instances = []

    class FakeBridge:
        def __init__(self, api_key, model, voice, instructions, emit, on_transcript):
            self.emit = emit
            self.requests = []
            self.trace_sink = None
            instances.append(self)

        async def connect(self):
            await self.emit({"type": "ready"})

        def update_context(self, instructions):
            self.instructions = instructions

        def request_response(self, instructions, priority=50, interrupt=False):
            self.requests.append((instructions, priority, interrupt))

        def append_audio(self, value):
            return False

        def append_video(self, value):
            return False

        def cancel_response(self, **kwargs):
            return False

        def set_trace_sink(self, sink):
            self.trace_sink = sink

        async def close(self):
            return None

    async def classify(_self, text, catalog):
        return {
            "method": "semantic-turn-router-v4",
            "model": "test-router",
            "status": "classified",
            "speechControl": None,
            "taskControl": None,
            "preferenceIntent": "propose-durable" if text.startswith("以后") else None,
            "memoryQuery": None,
            "memoryKind": "event" if text == "普通实验话语" else None,
            "memoryText": "用户完成了一次普通实验话语" if text == "普通实验话语" else None,
            "memoryKey": None,
            "memoryScope": "case" if text == "普通实验话语" else None,
            "memoryConsent": "observed" if text == "普通实验话语" else None,
            "skillControl": None,
            "selectedSkill": None,
            "skillStatus": "no-match",
            "rawOutput": "{}",
            "latencyMs": 0.1,
            "policyVersion": "semantic-turn-router-v4",
            "claimBoundary": "test classifier",
        }

    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setattr("egoassist.realtime.omni.OmniBrowserBridge", FakeBridge)
    monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client, "memory-policy-user")
            created = await (await client.post(
                "/api/sessions", json={"label": "B0 context-only"},
            )).json()
            session_id = created["session"]["id"]
            assert created["session"]["memoryPolicy"]["profile"] == "all-online"

            configured = await (await client.post(
                "/api/sessions/memory-policy",
                json={"sessionId": session_id, "policy": {"profile": "context-only"}},
            )).json()
            assert configured["memoryPolicy"]["profile"] == "context-only"

            ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await ws.receive_json())["type"] == "ready"
            initial = (await ws.receive_json())["state"]
            assert initial["memoryPolicy"]["profile"] == "context-only"
            assert Path(initial["episodicMemory"]["path"]).name == "timeline.sqlite3"
            assert initial["episodicMemory"]["retrieval"]["injection"]["status"] == "disabled"

            await ws.send_json({"type": "control", "transcript": "以后每次提醒我慢一点"})
            disabled_turn = [await ws.receive_json() for _ in range(3)]
            disabled_state = next(item["state"] for item in disabled_turn if item["type"] == "state")
            assert disabled_state["memory"]["candidate"] is None
            assert disabled_state["episodicMemory"]["count"] == 0

            enabled = await (await client.post(
                "/api/sessions/memory-policy",
                json={"sessionId": session_id, "policy": {"profile": "lexical-episodic"}},
            )).json()
            assert enabled["memoryPolicy"]["profile"] == "lexical-episodic"
            await ws.send_json({"type": "refresh"})
            assert (await ws.receive_json())["state"]["memoryPolicy"]["profile"] == "lexical-episodic"

            instances[0].trace_sink(
                "proactive.gate.decision",
                "realtime-bridge",
                {"sceneSummary": "案板上放着一把刀和蔬菜", "salientChange": False},
                [],
            )
            await ws.send_json({"type": "refresh"})
            scene_state = (await ws.receive_json())["state"]
            assert any(item["kind"] == "scene" for item in scene_state["episodicMemory"]["recent"])

            await ws.send_json({"type": "control", "transcript": "普通实验话语"})
            enabled_turn = [await ws.receive_json() for _ in range(3)]
            enabled_state = next(item["state"] for item in enabled_turn if item["type"] == "state")
            assert enabled_state["episodicMemory"]["count"] == 2

            preference_enabled = await (await client.post(
                "/api/sessions/memory-policy",
                json={"sessionId": session_id, "policy": {"profile": "confirmed-preference"}},
            )).json()
            assert preference_enabled["memoryPolicy"]["profile"] == "confirmed-preference"
            await ws.send_json({"type": "refresh"})
            assert (await ws.receive_json())["state"]["memoryPolicy"]["profile"] == "confirmed-preference"

            await ws.send_json({"type": "control", "transcript": "以后每次提醒我慢一点"})
            preference_turn = [await ws.receive_json() for _ in range(3)]
            preference_state = next(item["state"] for item in preference_turn if item["type"] == "state")
            assert preference_state["memory"]["candidate"]["status"] == "pending"
            confirmed = await client.post(
                "/api/candidate", json={"sessionId": session_id, "accepted": True},
            )
            assert confirmed.status == 200
            confirmed_state = await confirmed.json()
            record_id = confirmed_state["memory"]["records"][-1]["id"]
            deleted = await client.post(
                "/api/memory/delete", json={"sessionId": session_id, "id": record_id},
            )
            assert deleted.status == 200
            await ws.close()

            journal_path = tmp_path / "users" / user["id"] / "sessions" / f"{session_id}.jsonl"
            entries = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
            assert entries[0]["metadata"]["initialMemoryPolicy"]["profile"] == "all-online"
            changes = [item for item in entries if item.get("type") == "memory_policy_changed"]
            assert [item["policy"]["profile"] for item in changes] == [
                "context-only", "lexical-episodic", "confirmed-preference",
            ]
            memory_events = [
                item["eventType"] for item in entries
                if item.get("type") == "runtime_event" and str(item.get("eventType", "")).startswith(("memory.", "episodic.memory."))
            ]
            assert "episodic.memory.retrieved" in memory_events
            assert "episodic.memory.injected" in memory_events
            assert "episodic.memory.written" in memory_events
            assert "memory.candidate.proposed" in memory_events
            assert "memory.confirmed" in memory_events
            assert "memory.deleted" in memory_events

    asyncio.run(scenario())


def test_websocket_sessions_isolate_runtime_memory_and_resume_from_journal(tmp_path, monkeypatch):
    instances = []

    class FakeBridge:
        def __init__(self, api_key, model, voice, instructions, emit, on_transcript):
            self.emit = emit
            self.requests = []
            self.instructions = instructions
            instances.append(self)

        async def connect(self):
            await self.emit({"type": "ready"})

        def set_context_harness(self, harness):
            self.harness = harness

        def set_tools(self, declarations, handler):
            self.declarations = declarations
            self.tool_handler = handler

        def update_context(self, instructions):
            self.instructions = instructions

        def inject_context_data(self, label, value):
            return True

        def request_response(self, instructions, priority=50, interrupt=False):
            self.requests.append((instructions, priority, interrupt))

        def append_audio(self, value):
            return False

        def append_video(self, value):
            return False

        def cancel_response(self):
            return False

        async def close(self):
            return None

    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setattr("egoassist.realtime.omni.OmniBrowserBridge", FakeBridge)
    patch_semantic_router(monkeypatch)

    async def initial_messages(ws):
        ready = await ws.receive_json()
        state = await ws.receive_json()
        assert ready["contextSessionId"]
        return ready, state["state"]

    async def control(ws, transcript):
        await ws.send_json({"type": "control", "transcript": transcript})
        messages = [await ws.receive_json() for _ in range(3)]
        assert [item["type"] for item in messages] == ["user_transcript", "state", "interrupted"]
        return messages[1]["state"]

    async def scenario():
        memory_path = tmp_path / "memory.json"
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(memory_path), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client)
            first = await client.ws_connect("/ws/realtime?session=first-session")
            second = await client.ws_connect("/ws/realtime?session=second-session")
            await initial_messages(first)
            await initial_messages(second)
            assert "<available_skills>" in instances[0].instructions
            assert "kitchen-coach" in instances[0].instructions
            assert "assembly-coach" in instances[0].instructions
            assert "Never silently substitute the default recipe" not in instances[0].instructions
            assert instances[0].declarations == []
            with pytest.raises(ValueError, match="not available"):
                instances[0].tool_handler(
                    "activate_skill",
                    {"skill": "kitchen-coach", "evidence": "做宫保鸡丁"},
                    False,
                )
            with pytest.raises(WSServerHandshakeError) as duplicate_error:
                await client.ws_connect("/ws/realtime?session=first-session")
            assert duplicate_error.value.status == 409

            first_state = await control(first, "cook fruit salad")
            second_state = await control(second, "assemble flat pack stool")
            assert {item["name"] for item in instances[0].declarations} == {
                "set_timer", "request_human_help",
            }
            assert "activate_skill" not in {item["name"] for item in instances[0].declarations}
            assert first_state["activeSkill"]["name"] == "kitchen-coach"
            assert first_state["task"]["task"]["id"] == "fruit-salad"
            assert second_state["activeSkill"]["name"] == "assembly-coach"
            assert second_state["task"]["task"]["id"] == "flat-pack-stool"

            await first.send_json({"type": "refresh"})
            refreshed = (await first.receive_json())["state"]
            assert refreshed["task"]["task"]["id"] == "fruit-salad"
            recent = json.dumps(refreshed["episodicMemory"]["recent"], ensure_ascii=False)
            assert recent == "[]"
            assert "assemble flat pack stool" not in recent

            await first.close()
            await second.close()

            resumed = await client.ws_connect("/ws/realtime?session=first-session")
            ready, resumed_state = await initial_messages(resumed)
            assert ready["contextSessionId"] == "first-session"
            assert resumed_state["activeSkill"]["name"] == "kitchen-coach"
            assert resumed_state["task"]["task"]["id"] == "fruit-salad"

            exited_state = await control(resumed, "exit current skill")
            assert exited_state["activeSkill"] is None
            assert exited_state["task"] is None
            assert exited_state["intentRouting"]["status"] == "deactivated"
            await resumed.close()

            general = await client.ws_connect("/ws/realtime?session=first-session")
            _, general_state = await initial_messages(general)
            assert general_state["activeSkill"] is None
            assert general_state["task"] is None
            await general.close()

        sessions = tmp_path / "users" / user["id"] / "sessions"
        first_log = (sessions / "first-session.jsonl").read_text(encoding="utf-8")
        second_log = (sessions / "second-session.jsonl").read_text(encoding="utf-8")
        assert "cook fruit salad" in first_log and "assemble flat pack stool" not in first_log
        assert "assemble flat pack stool" in second_log and "cook fruit salad" not in second_log
        first_entries = [json.loads(line) for line in first_log.splitlines()]
        lifecycle = [
            item for item in first_entries
            if item.get("type") == "runtime_event"
            and item.get("eventType") == "runtime.skill.deactivated"
        ]
        assert lifecycle
        checkpoints = [item for item in first_entries if item.get("type") == "state_checkpoint"]
        assert checkpoints[-1]["state"]["skill"] is None
        assert checkpoints[-1]["state"]["task"] is None

    asyncio.run(scenario())


def test_realtime_transcripts_are_classified_and_committed_in_fifo_order(tmp_path, monkeypatch):
    instances = install_queue_test_bridge(monkeypatch)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    classifier_calls = []

    async def classify(_self, text, catalog):
        classifier_calls.append(text)
        if text == "first turn":
            first_started.set()
            await release_first.wait()
        return semantic_result(text)

    monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client)
            session_id = (await (await client.post("/api/sessions", json={"label": "fifo"})).json())["session"]["id"]
            ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await ws.receive_json())["type"] == "ready"
            assert (await ws.receive_json())["type"] == "state"

            instances[0].on_transcript("first turn")
            await asyncio.wait_for(first_started.wait(), timeout=1)
            instances[0].on_transcript("second turn")
            await asyncio.sleep(0)
            assert classifier_calls == ["first turn"]

            release_first.set()
            assert (await asyncio.wait_for(ws.receive_json(), timeout=1))["type"] == "state"
            assert (await asyncio.wait_for(ws.receive_json(), timeout=1))["type"] == "state"
            assert classifier_calls == ["first turn", "second turn"]
            assert "first turn" in instances[0].requests[0][0]
            assert "second turn" in instances[0].requests[1][0]
            assert [item[0] for item in instances[0].injections[-2:]] == [
                "current_turn_context", "current_turn_context",
            ]
            await ws.close()

        journal = tmp_path / "users" / user["id"] / "sessions" / f"{session_id}.jsonl"
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        inputs = [item["input"] for item in entries if item.get("type") == "control_decision"]
        assert inputs[-2:] == ["first turn", "second turn"]

    asyncio.run(scenario())


def test_reset_cancels_pending_semantic_turn_before_durable_deactivation(tmp_path, monkeypatch):
    instances = install_queue_test_bridge(monkeypatch)
    classification_started = asyncio.Event()
    classification_cancelled = asyncio.Event()
    release_classification = asyncio.Event()
    release_cancellation = asyncio.Event()

    async def classify(_self, text, catalog):
        classification_started.set()
        try:
            await release_classification.wait()
        except asyncio.CancelledError:
            classification_cancelled.set()
            await release_cancellation.wait()
            raise
        return semantic_result(text, selected_skill="assembly-coach")

    monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client)
            session_id = (await (await client.post("/api/sessions", json={"label": "reset-race"})).json())["session"]["id"]
            activated = await (await client.post(
                "/api/skills/activate", json={"skill": "assembly-coach", "sessionId": session_id},
            )).json()
            assert activated["activeSkill"]["name"] == "assembly-coach"
            ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
            await ws.receive_json()
            await ws.receive_json()

            instances[0].on_transcript("slow old turn")
            await asyncio.wait_for(classification_started.wait(), timeout=1)
            async def reset_case():
                return await (await client.post(
                    "/api/task/reset", json={"sessionId": session_id},
                )).json()

            reset_task = asyncio.create_task(reset_case())
            await asyncio.wait_for(classification_cancelled.wait(), timeout=1)
            with pytest.raises(WSServerHandshakeError) as transition_error:
                await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert transition_error.value.status == 409
            release_cancellation.set()
            reset = await asyncio.wait_for(reset_task, timeout=1)
            assert reset["activeSkill"] is None
            release_classification.set()

            restored = await (await client.get(f"/api/state?session={session_id}")).json()
            assert restored["activeSkill"] is None
            assert restored["task"] is None

        journal = tmp_path / "users" / user["id"] / "sessions" / f"{session_id}.jsonl"
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        checkpoints = [item for item in entries if item.get("type") == "state_checkpoint"]
        assert checkpoints[-1]["state"]["skill"] is None
        assert not any(
            item.get("type") == "message" and item.get("message", {}).get("content") == "slow old turn"
            for item in entries
        )

    asyncio.run(scenario())


def test_simultaneous_websocket_handshakes_reserve_exactly_one_writer(tmp_path, monkeypatch):
    install_queue_test_bridge(monkeypatch)
    patch_semantic_router(monkeypatch)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            session_id = (await (await client.post("/api/sessions", json={"label": "single-writer"})).json())["session"]["id"]
            results = await asyncio.gather(
                client.ws_connect(f"/ws/realtime?session={session_id}"),
                client.ws_connect(f"/ws/realtime?session={session_id}"),
                return_exceptions=True,
            )
            sockets = [item for item in results if not isinstance(item, BaseException)]
            failures = [item for item in results if isinstance(item, WSServerHandshakeError)]
            assert len(sockets) == 1
            assert len(failures) == 1
            assert failures[0].status == 409
            await sockets[0].close()

    asyncio.run(scenario())


def test_case_observation_tool_audit_and_routing_survive_state_and_reconnect(tmp_path, monkeypatch):
    install_queue_test_bridge(monkeypatch)
    patch_semantic_router(monkeypatch)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            session_id = (await (await client.post("/api/sessions", json={"label": "case-api"})).json())["session"]["id"]
            await client.post(
                "/api/skills/activate", json={"skill": "kitchen-coach", "sessionId": session_id},
            )
            await client.post(
                "/api/tasks/select", json={"task": "fruit-salad", "sessionId": session_id},
            )
            observed = await (await client.post("/api/task/observe", json={
                "sessionId": session_id,
                "step": "clean",
                "completed": False,
                "confidence": 0.92,
                "summary": "case-only observation",
                "hazards": [],
                "source": "test",
            })).json()
            assert observed["task"]["observation"]["summary"] == "case-only observation"
            replayed = await (await client.post(
                "/api/replay/observe", json={"sessionId": session_id, "kind": "progress"},
            )).json()
            assert replayed["task"]["observation"]["source"] == "research-replay/deterministic"

            tool = await (await client.post("/api/tools/execute", json={
                "sessionId": session_id,
                "tool": "set_timer",
                "arguments": {"seconds": 5, "label": "fruit"},
            })).json()
            assert tool["state"]["toolAudit"][0]["tool"] == "set_timer"
            refreshed = await (await client.get(f"/api/state?session={session_id}")).json()
            assert refreshed["toolAudit"][0]["tool"] == "set_timer"

            ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await ws.receive_json())["type"] == "ready"
            resumed = (await ws.receive_json())["state"]
            assert resumed["intentRouting"]["selectedSkill"] == "kitchen-coach"
            assert resumed["toolAudit"][0]["tool"] == "set_timer"
            await ws.close()

        restarted_app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(restarted_app)) as restarted_client:
            login = await restarted_client.post("/api/auth/login", json={
                "username": "researcher", "password": "baseline-pass-123",
            })
            assert login.status == 200
            restored = await (await restarted_client.get(f"/api/state?session={session_id}")).json()
            assert restored["intentRouting"]["selectedSkill"] == "kitchen-coach"
            assert restored["toolAudit"][0]["tool"] == "set_timer"

    asyncio.run(scenario())


def test_logout_closes_authenticated_websocket_and_case_state_exposes_journal(tmp_path, monkeypatch):
    install_queue_test_bridge(monkeypatch)
    patch_semantic_router(monkeypatch)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            session_id = (await (await client.post(
                "/api/sessions", json={"label": "logout"},
            )).json())["session"]["id"]
            activated = await client.post(
                "/api/skills/activate",
                json={"skill": "kitchen-coach", "sessionId": session_id},
            )
            assert activated.status == 200
            state = await (await client.get(f"/api/state?session={session_id}")).json()
            assert state["events"]
            assert state["promptContext"]["harnesses"]
            assert state["sessionId"] == session_id

            ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await ws.receive_json())["type"] == "ready"
            assert (await ws.receive_json())["type"] == "state"
            logout = await client.post("/api/auth/logout")
            assert logout.status == 200
            assert (await ws.receive_json())["type"] == "session_stale"

    asyncio.run(scenario())


def test_websocket_initialization_failure_releases_case_reservation(tmp_path, monkeypatch):
    install_queue_test_bridge(monkeypatch)
    patch_semantic_router(monkeypatch)
    original_build = run_egoassist.ContextHarness.build
    attempts = 0

    def fail_once(self, layers):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic harness failure")
        return original_build(self, layers)

    monkeypatch.setattr(run_egoassist.ContextHarness, "build", fail_once)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            session_id = (await (await client.post(
                "/api/sessions", json={"label": "retry"},
            )).json())["session"]["id"]
            failed = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await failed.receive_json())["type"] == "error"
            retried = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await retried.receive_json())["type"] == "ready"
            assert (await retried.receive_json())["type"] == "state"
            await retried.close()

    asyncio.run(scenario())


def test_runtime_restore_does_not_append_synthetic_task_events(tmp_path):
    async def scenario():
        memory_path = tmp_path / "memory.json"
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(memory_path), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client)
            session_id = (await (await client.post(
                "/api/sessions", json={"label": "silent-restore"},
            )).json())["session"]["id"]
            await client.post(
                "/api/skills/activate",
                json={"skill": "kitchen-coach", "sessionId": session_id},
            )
            await client.post(
                "/api/tasks/select",
                json={"task": "fruit-salad", "sessionId": session_id},
            )

        journal_path = tmp_path / "users" / user["id"] / "sessions" / f"{session_id}.jsonl"
        before = journal_path.read_text(encoding="utf-8").splitlines()
        restarted = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(memory_path), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(restarted)) as client:
            login = await client.post("/api/auth/login", json={
                "username": "researcher", "password": "baseline-pass-123",
            })
            assert login.status == 200
            state = await client.get(f"/api/state?session={session_id}")
            assert state.status == 200
        after = journal_path.read_text(encoding="utf-8").splitlines()
        assert after == before

    asyncio.run(scenario())


def test_active_case_owner_cache_fails_bounded_instead_of_growing(tmp_path, monkeypatch):
    install_queue_test_bridge(monkeypatch)
    patch_semantic_router(monkeypatch)
    monkeypatch.setenv("EGOASSIST_MAX_CASE_OWNERS", "2")

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            ids = []
            for label in ("one", "two", "three"):
                created = await (await client.post("/api/sessions", json={"label": label})).json()
                ids.append(created["session"]["id"])
            sockets = []
            for session_id in ids[:2]:
                ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
                await ws.receive_json()
                await ws.receive_json()
                sockets.append(ws)
            overloaded = await client.get(f"/api/state?session={ids[2]}")
            assert overloaded.status == 503
            for ws in sockets:
                await ws.close()

    asyncio.run(scenario())


def test_provider_tool_callback_is_blocked_after_case_transition(tmp_path, monkeypatch):
    instances = install_queue_test_bridge(monkeypatch)
    classification_started = asyncio.Event()
    classification_cancelled = asyncio.Event()
    release_cancellation = asyncio.Event()

    async def classify(_self, text, catalog):
        classification_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            classification_cancelled.set()
            await release_cancellation.wait()
            raise

    monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)

    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            user = await register_client(client)
            session_id = (await (await client.post(
                "/api/sessions", json={"label": "tool-race"},
            )).json())["session"]["id"]
            await client.post(
                "/api/skills/activate",
                json={"skill": "kitchen-coach", "sessionId": session_id},
            )
            ws = await client.ws_connect(f"/ws/realtime?session={session_id}")
            await ws.receive_json()
            await ws.receive_json()
            instances[0].on_transcript("hold transition lock")
            await asyncio.wait_for(classification_started.wait(), timeout=1)
            reset_task = asyncio.create_task(client.post(
                "/api/task/reset", json={"sessionId": session_id},
            ))
            await asyncio.wait_for(classification_cancelled.wait(), timeout=1)
            tool_task = asyncio.create_task(asyncio.to_thread(
                instances[0].tool_handler,
                "set_timer",
                {"seconds": 5, "label": "stale"},
                False,
            ))
            await asyncio.sleep(0)
            assert not tool_task.done()
            release_cancellation.set()
            reset_response = await asyncio.wait_for(reset_task, timeout=1)
            assert reset_response.status == 200
            with pytest.raises(RuntimeError, match="stale case connection"):
                await asyncio.wait_for(tool_task, timeout=1)

        journal = tmp_path / "users" / user["id"] / "sessions" / f"{session_id}.jsonl"
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        assert not any(item.get("type") == "tool_audit" for item in entries)

    asyncio.run(scenario())
