import asyncio
import json
from types import SimpleNamespace

from egoassist.realtime import SemanticTurnRouter, TURN_ROUTER_POLICY_VERSION


def provider_response(content, status_code=200):
    return SimpleNamespace(
        status_code=status_code,
        output=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=content)),
        ]),
        message="provider error",
    )


def decision(**overrides):
    value = {
        "speechControl": "none",
        "taskControl": "none",
        "preferenceIntent": "none",
        "memoryQuery": "none",
        "skill": "none",
    }
    value.update(overrides)
    return json.dumps(value)


def test_semantic_control_has_no_local_phrase_override():
    calls = []

    def generation_call(**kwargs):
        calls.append(kwargs)
        return provider_response(decision())

    router = SemanticTurnRouter("test-key", model="test-router", generation_call=generation_call)
    result = asyncio.run(router.classify("您别说了。", []))

    assert result["speechControl"] is None
    assert result["status"] == "classified"
    assert result["policyVersion"] == TURN_ROUTER_POLICY_VERSION
    assert calls[0]["temperature"] == 0
    assert calls[0]["messages"][1] == {"role": "user", "content": "您别说了。"}


def test_semantic_control_accepts_a_valid_stop_decision():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(
            decision(speechControl="stop-speaking"),
        ),
    )

    result = asyncio.run(router.classify("请结束当前的语音输出", []))

    assert result["speechControl"] == "stop-speaking"
    assert result["selectedSkill"] is None


def test_semantic_router_rejects_unconstrained_output_and_fails_open():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response("stop-speaking"),
    )

    result = asyncio.run(router.classify("一条普通话语", []))

    assert result["status"] == "invalid"
    assert result["speechControl"] is None
    assert result["taskControl"] is None
    assert result["selectedSkill"] is None


def test_semantic_router_validates_skill_against_disclosed_catalog():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(
            decision(skill="kitchen-coach"),
        ),
    )
    catalog = [{"name": "kitchen-coach", "description": "Cooking guidance"}]

    result = asyncio.run(router.classify("我要做一道菜", catalog))

    assert result["skillStatus"] == "matched"
    assert result["selectedSkill"] == "kitchen-coach"


def test_semantic_router_exposes_all_auditable_turn_labels():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(decision(
            taskControl="repeat",
            preferenceIntent="propose-durable",
            memoryQuery="referential",
        )),
    )

    result = asyncio.run(router.classify("test evidence only", []))

    assert result["taskControl"] == "repeat"
    assert result["preferenceIntent"] == "propose-durable"
    assert result["memoryQuery"] == "referential"


def test_semantic_router_rejects_extra_fields():
    value = json.loads(decision())
    value["explanation"] = "should not be accepted"
    router = SemanticTurnRouter(
        "test-key", generation_call=lambda **_: provider_response(json.dumps(value)),
    )

    result = asyncio.run(router.classify("test", []))

    assert result["status"] == "invalid"
    assert result["speechControl"] is None


def test_semantic_router_timeout_fails_open_without_state_labels():
    async def scenario():
        router = SemanticTurnRouter(
            "test-key", timeout_seconds=0.001,
            generation_call=lambda **_: __import__("time").sleep(0.05),
        )
        return await router.classify("test", [])

    result = asyncio.run(scenario())

    assert result["status"] == "unavailable"
    assert result["speechControl"] is None
    assert result["taskControl"] is None
    assert result["preferenceIntent"] is None
    assert result["memoryQuery"] is None
