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
        "memoryKind": "none",
        "memoryText": "none",
        "memoryKey": "none",
        "memoryScope": "none",
        "memoryConsent": "none",
        "skillControl": "none",
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


def test_semantic_router_accepts_typed_skill_deactivation():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(
            decision(skillControl="deactivate"),
        ),
    )

    result = asyncio.run(router.classify("return to general mode", []))

    assert result["skillControl"] == "deactivate"
    assert result["selectedSkill"] is None


def test_semantic_router_rejects_deactivation_with_skill_selection():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(
            decision(skillControl="deactivate", skill="kitchen-coach"),
        ),
    )
    catalog = [{"name": "kitchen-coach", "description": "Cooking guidance"}]

    result = asyncio.run(router.classify("conflicting control", catalog))

    assert result["status"] == "invalid"
    assert result["skillControl"] is None
    assert result["selectedSkill"] is None


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


def test_semantic_router_accepts_explicit_participant_fact_card():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(decision(
            memoryKind="fact",
            memoryText="小李的电话号码是13657352908",
            memoryKey="contact:小李:phone",
            memoryScope="participant",
            memoryConsent="explicit",
        )),
    )

    result = asyncio.run(router.classify("帮我记住，小李电话是13657352908", []))

    assert result["memoryKind"] == "fact"
    assert result["memoryText"] == "小李的电话号码是13657352908"
    assert result["memoryConsent"] == "explicit"


def test_semantic_router_rejects_question_disguised_as_memory_card_scope():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(decision(
            memoryKind="event",
            memoryText="用户今天吃过药",
            memoryScope="participant",
            memoryConsent="observed",
        )),
    )

    result = asyncio.run(router.classify("我今天吃过药了吗？", []))

    assert result["status"] == "invalid"
    assert result["memoryKind"] is None


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
    assert result["memoryKind"] is None
