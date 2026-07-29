import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, CookieJar
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.client_exceptions import WSServerHandshakeError

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.realtime import PreferenceMemory
import run_egoassist
from run_egoassist import create_app


ROOT = Path(__file__).parents[1]


def patch_semantic_router(monkeypatch):
    async def classify(_self, text, catalog):
        stop = text in {"您别说了。", "请停止当前语音输出"}
        selected_skill = (
            "kitchen-coach" if text == "cook fruit salad"
            else "assembly-coach" if text == "assemble flat pack stool"
            else None
        )
        return {
            "method": "semantic-turn-router-v2",
            "model": "test-router",
            "status": "classified",
            "speechControl": "stop-speaking" if stop else None,
            "taskControl": "repeat" if text == "再说一遍" else None,
            "preferenceIntent": None,
            "memoryQuery": None,
            "selectedSkill": selected_skill,
            "skillStatus": "matched" if selected_skill else "no-match",
            "rawOutput": json.dumps({
                "speechControl": "stop-speaking" if stop else "none",
                "taskControl": "repeat" if text == "再说一遍" else "none",
                "preferenceIntent": "none", "memoryQuery": "none",
                "skill": selected_skill or "none",
            }),
            "latencyMs": 0.1,
            "policyVersion": "semantic-turn-router-v2",
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
            assert selected["episodicMemory"]["count"] == 2
            assert selected["episodicMemory"]["retainsRawMedia"] is False

            reset = await (await client.post("/api/task/reset", json={})).json()
            assert reset["activeSkill"] is None
            assert reset["task"] is None
            assert reset["intentRouting"]["status"] == "no-match"
            assert reset["intentRouting"]["selectedSkill"] is None

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
                await alice_client.post(base + "/api/voice", json={"transcript": "记住一段数字"})
                await alice_client.post(base + "/api/voice", json={"transcript": "四五六七八九二"})

                bob_state = await (await bob_client.get(base + "/api/state")).json()
                assert bob_state["activeSkill"] is None
                assert bob_state["memory"]["records"] == []
                assert alice_state["episodicMemory"]["path"] != bob_state["episodicMemory"]["path"]
                assert alice["id"] in alice_state["episodicMemory"]["path"]
                assert bob["id"] in bob_state["episodicMemory"]["path"]
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
                case_root = tmp_path / "users" / alice["id"] / "cases" / alice_session
                assert Path(scoped_state["memory"]["memoryPath"]) == case_root / "preferences.json"
                assert Path(scoped_state["episodicMemory"]["path"]) == case_root / "episodes.json"
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
            await client.post("/api/voice", json={"transcript": "记住一段数字"})
            await client.post("/api/voice", json={"transcript": "四五六七八九二"})
            recalled = await (await client.post(
                "/api/voice", json={
                    "transcript": "我刚才让你记住的数字是什么来着",
                    "memoryQuery": "referential",
                },
            )).json()

            injection = recalled["episodicMemory"]["retrieval"]["injection"]
            assert injection["status"] == "injected"
            assert any(item["summary"] == "四五六七八九二" for item in injection["evidence"])
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
            memory = PreferenceMemory(tmp_path / "users" / user["id"] / "preferences.json")
            memory.propose_preference(
                task="tomato-eggs", step="prepare", prompt="开始前提醒我把动作说慢一点。",
                evidence="用户明确确认的语音节奏偏好",
            )
            memory.resolve_candidate(True)
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
            assert explicit_stop[1]["state"]["intentRouting"]["method"] == "semantic-turn-router-v2"
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

            journal = tmp_path / "users" / user["id"] / "sessions" / f"{ready['contextSessionId']}.jsonl"
            entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            decision = next(
                item for item in reversed(entries)
                if item.get("type") == "control_decision"
            )
            assert decision["decision"]["speechControl"] == "stop-speaking"
            assert decision["decision"]["method"] == "semantic-turn-router-v2"

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
            "method": "semantic-turn-router-v2",
            "model": "test-router",
            "status": "classified",
            "speechControl": None,
            "taskControl": None,
            "preferenceIntent": "propose-durable" if text.startswith("以后") else None,
            "memoryQuery": None,
            "selectedSkill": None,
            "skillStatus": "no-match",
            "rawOutput": "{}",
            "latencyMs": 0.1,
            "policyVersion": "semantic-turn-router-v2",
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
            assert session_id in initial["episodicMemory"]["path"]
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
            assert "cook fruit salad" in recent
            assert "assemble flat pack stool" not in recent

            await first.close()
            await second.close()

            resumed = await client.ws_connect("/ws/realtime?session=first-session")
            ready, resumed_state = await initial_messages(resumed)
            assert ready["contextSessionId"] == "first-session"
            assert resumed_state["activeSkill"]["name"] == "kitchen-coach"
            assert resumed_state["task"]["task"]["id"] == "fruit-salad"
            await resumed.close()

        sessions = tmp_path / "users" / user["id"] / "sessions"
        first_log = (sessions / "first-session.jsonl").read_text(encoding="utf-8")
        second_log = (sessions / "second-session.jsonl").read_text(encoding="utf-8")
        assert "cook fruit salad" in first_log and "assemble flat pack stool" not in first_log
        assert "assemble flat pack stool" in second_log and "cook fruit salad" not in second_log

    asyncio.run(scenario())
