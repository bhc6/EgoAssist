import asyncio
import json
from types import SimpleNamespace

from egoassist.realtime import (
    SemanticTurnRouter, TURN_ROUTER_POLICY_VERSION, explicit_memory_fallback_atoms,
    is_explicit_memory_request, memory_draft_candidate, normalize_intent_candidates,
)


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


def test_semantic_router_accepts_background_intent_lifecycle_label():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(
            decision(intentKind="background-reminder"),
        ),
    )
    result = asyncio.run(router.classify("remind me later", []))
    assert result["intentKind"] == "background-reminder"


def test_legacy_semantic_output_defaults_to_foreground_intent():
    router = SemanticTurnRouter(
        "test-key", generation_call=lambda **_: provider_response(decision()),
    )
    result = asyncio.run(router.classify("ordinary turn", []))
    assert result["intentKind"] == "foreground-dialogue"


def test_semantic_router_accepts_atomic_foreground_and_reminder_intents():
    router = SemanticTurnRouter(
        "test-key",
        generation_call=lambda **_: provider_response(decision(
            intentKind="foreground-dialogue",
            intents=[
                {
                    "kind": "background-reminder",
                    "text": "remind me in 5 minutes to turn off the oven",
                    "parameters": {"delaySeconds": 300, "label": "turn off the oven"},
                },
                {
                    "kind": "foreground-dialogue",
                    "text": "what temperature should I use",
                    "parameters": {},
                },
            ],
        )),
    )

    result = asyncio.run(router.classify(
        "remind me in 5 minutes to turn off the oven, and what temperature should I use?", [],
    ))

    assert result["status"] == "classified"
    assert [item["kind"] for item in result["intents"]] == [
        "background-reminder", "foreground-dialogue",
    ]
    assert result["intents"][0]["parameters"]["delaySeconds"] == 300


def test_intent_decomposition_parses_relative_delay_but_never_invents_one():
    parsed = normalize_intent_candidates(
        "remind me in 2 minutes to check the rice",
        {"intentKind": "background-reminder"},
    )
    ambiguous = normalize_intent_candidates(
        "remind me to check the rice",
        {"intentKind": "background-reminder"},
    )

    assert parsed[0].parameters == {"delaySeconds": 120, "label": "check the rice"}
    assert ambiguous[0].parameters == {"label": "check the rice"}


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


def test_structured_router_emits_three_atoms_draft_and_entity_query():
    payload = json.loads(decision())
    payload.update({
        "memoryAtoms": [
            {
                "kind": "state", "text": "维生素D3放在书桌右边的架子上。",
                "subject": "维生素D3", "predicate": "location", "value": "书桌右边的架子",
                "scope": "participant", "consent": "explicit",
            },
            {
                "kind": "preference", "text": "用户每天中午十二点服用两颗维生素D3。",
                "subject": "用户", "predicate": "medication.schedule", "value": "维生素D3两颗",
                "scope": "participant", "consent": "explicit",
            },
        ],
        "memoryDraft": None,
        "memoryQuerySpec": {"subject": "维生素D3", "predicate": "location"},
    })
    router = SemanticTurnRouter(
        "test-key", generation_call=lambda **_: provider_response(json.dumps(payload, ensure_ascii=False)),
    )
    router.set_turn_context(
        clock_context={"localNow": "2026-07-29T21:00:00+08:00", "timezone": "Asia/Shanghai"},
        memory_draft=None,
    )

    result = asyncio.run(router.classify("请记住药的位置和服药时间", []))

    assert result["status"] == "classified"
    assert len(result["memoryAtoms"]) == 2
    assert result["memoryText"] == "维生素D3放在书桌右边的架子上。"
    assert result["memoryQuerySpec"] == {"subject": "维生素D3", "predicate": "location"}


def test_structured_router_supplies_clock_and_pending_draft_to_provider():
    calls = []

    def generation_call(**kwargs):
        calls.append(kwargs)
        return provider_response(decision())

    router = SemanticTurnRouter("test-key", generation_call=generation_call)
    router.set_turn_context(
        clock_context={"localNow": "2026-07-29T21:00:00+08:00", "timezone": "Asia/Shanghai"},
        memory_draft={"subject": "小李", "predicate": "phone", "missingFields": ["value"]},
    )
    asyncio.run(router.classify("是13659892706", []))
    supplied = json.loads(calls[0]["messages"][1]["content"])
    assert supplied["transcript"] == "是13659892706"
    assert supplied["clock"]["timezone"] == "Asia/Shanghai"
    assert supplied["pendingMemoryDraft"]["subject"] == "小李"


def test_server_memory_intent_gate_rejects_questions_and_unrelated_drafts():
    assert is_explicit_memory_request("你帮我记几个电话吧。") is True
    assert is_explicit_memory_request("我最后再说一遍，你必须记住这个位置。") is True
    assert is_explicit_memory_request("你现在记住了吗？") is False
    assert is_explicit_memory_request("我手上拿的是什么？") is False

    unrelated, reason = memory_draft_candidate(
        "我手上拿的是什么？",
        {"mode": "start", "subject": None, "predicate": "phone", "missingFields": ["subject", "value"]},
        {"subject": None, "predicate": "phone", "missingFields": ["subject", "value"]},
    )
    assert unrelated is None
    assert reason == "draft-start-without-explicit-memory-request"


def test_server_memory_draft_continuation_requires_typed_progress():
    pending = {"subject": None, "predicate": "phone", "missingFields": ["subject", "value"]}
    accepted, reason = memory_draft_candidate(
        "是小李的电话",
        {"mode": "continue", "subject": "小李", "predicate": "phone", "missingFields": ["value"]},
        pending,
    )
    assert accepted["subject"] == "小李"
    assert reason is None

    rejected, reason = memory_draft_candidate(
        "你看我做得对吗？",
        {"mode": "continue", "subject": None, "predicate": "phone", "missingFields": ["subject", "value"]},
        pending,
    )
    assert rejected is None
    assert reason == "draft-continuation-made-no-progress"


def test_explicit_memory_fallback_extracts_failed_trial_phone_atoms():
    atoms = explicit_memory_fallback_atoms(
        "呃，小陈的电话是幺三六五七八九五六三二，小李的电话是二六三五七八。"
        "然后我们这里的报警电话是九幺幺，你都要记住。",
        {"subject": None, "predicate": "phone", "missingFields": ["subject", "value"]},
    )
    assert [(item["subject"], item["predicate"], item["value"]) for item in atoms] == [
        ("小陈", "phone", "13657895632"),
        ("小李", "phone", "263578"),
        ("报警电话", "phone", "911"),
    ]
    assert all(item["scope"] == "participant" for item in atoms)


def test_explicit_memory_fallback_keeps_location_and_schedule_separate():
    atoms = explicit_memory_fallback_atoms(
        "每天中午十二点提醒我吃开瑞坦，开瑞坦放在书桌右边的架子上。",
        {"subject": None, "predicate": "memory.value", "missingFields": ["subject", "value"]},
    )
    assert {(item["predicate"], item["value"]) for item in atoms} == {
        ("medication.schedule", "每天中午十二点服用开瑞坦"),
        ("location", "书桌右边的架子上"),
    }


def test_explicit_memory_fallback_associates_number_before_subject():
    atoms = explicit_memory_fallback_atoms(
        "幺二三四五六七，记住这个号码，这是小陈的小号。",
    )
    assert [(item["subject"], item["predicate"], item["value"]) for item in atoms] == [
        ("小陈", "phone", "1234567"),
    ]
