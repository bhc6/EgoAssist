import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.realtime import PreferenceMemory
import run_egoassist
from run_egoassist import create_app


ROOT = Path(__file__).parents[1]


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


def test_api_switches_catalog_tasks_and_isolates_custom_tasks(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
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


def test_voice_repeat_is_json_and_only_explicit_durable_preferences_create_memory(tmp_path):
    async def scenario():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            repeated_response = await client.post("/api/voice", json={"transcript": "再说一遍"})
            assert repeated_response.status == 200
            repeated = await repeated_response.json()
            assert repeated["task"]["decision"]["reason"] == "用户请求重复"
            assert repeated["memory"]["candidate"] is None

            preference = await (await client.post(
                "/api/voice", json={"transcript": "以后每次到这里请提醒我慢一点"},
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
            self.requests = []
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

        def cancel_response(self):
            return False

        async def close(self):
            return None

    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setattr("egoassist.realtime.omni.OmniBrowserBridge", FakeBridge)

    async def scenario():
        memory_path = tmp_path / "memory.json"
        memory = PreferenceMemory(memory_path)
        memory.propose_preference(
            task="tomato-eggs", step="prepare", prompt="开始前提醒我把动作说慢一点。",
            evidence="用户明确确认的语音节奏偏好",
        )
        memory.resolve_candidate(True)
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), "kitchen-coach",
            str(memory_path), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws/realtime")
            assert (await ws.receive_json())["type"] == "ready"
            initial_state = await ws.receive_json()
            assert initial_state["type"] == "state"
            assert initial_state["state"]["memoryMechanism"]["impact"]["status"] == "reused"
            assert instances[0].requests[0][1] == 80
            assert "用户明确确认的长期记忆" in instances[0].requests[0][0]
            instances[0].requests.clear()
            await ws.send_json({"type": "control", "transcript": "再说一遍"})
            messages = [await ws.receive_json() for _ in range(3)]
            assert [item["type"] for item in messages] == ["user_transcript", "state", "interrupted"]
            assert messages[1]["state"]["memory"]["candidate"] is None
            assert instances[0].requests[0][1:] == (100, True)
            assert "上一条实际说出的指导" in instances[0].requests[0][0]
            await ws.close()

    asyncio.run(scenario())
