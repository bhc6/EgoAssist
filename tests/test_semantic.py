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


def test_read_only_query_envelope_keeps_source_time_context_and_audits_normalization():
    payload = {"v": 1, "memoryQuery": {"referential": True, "context": {
        "entities": ["张岚"], "times": [
            {"relation": "讨论", "expression": "昨天", "anchor": "turn"},
            {"relation": "约定", "expression": "前天", "anchor": "turn"},
        ],
    }}}
    router = SemanticTurnRouter("test-key", generation_call=lambda **_: provider_response(json.dumps(payload)))
    result = asyncio.run(router.classify("张岚昨天讨论的前天的约定是什么", []))
    assert result["status"] == "classified"
    assert result["memoryQuery"] == "referential"
    assert len(result["memoryQuerySpec"]["context"]["times"]) == 2
    assert result["protocolNormalizations"] == ["memoryQuery-object-to-spec"]
    assert result["memoryAtoms"] == []
    payload["memoryQuery"]["delete"] = "all"
    rejected = asyncio.run(router.classify("张岚昨天讨论的前天的约定是什么", []))
    assert rejected["status"] == "invalid"


def test_compact_event_atom_preserves_source_without_forcing_one_relation():
    payload = {"v": 1, "memoryEvents": [{"scope": "participant", "consent": "explicit",
               "context": {"entities": ["张岚", "李宁"], "scene": "咖啡馆"}}]}
    text = "请记住：昨天张岚和李宁在咖啡馆讨论了前天的约定。"
    router = SemanticTurnRouter("test-key", generation_call=lambda **_: provider_response(json.dumps(payload)))
    result = asyncio.run(router.classify(text, []))
    assert result["status"] == "classified"
    assert result["memoryAtoms"][0]["text"] == text
    assert result["memoryAtoms"][0]["subject"] is None
    assert result["memoryAtoms"][0]["predicate"] is None
    assert result["protocolNormalizations"] == ["memoryEvents-to-source-events"]
    payload.update(text="fabricated outside the event envelope", subject="invented")
    normalized = asyncio.run(router.classify(text, []))
    assert normalized["memoryAtoms"][0]["text"] == text
    assert normalized["memoryAtoms"][0]["subject"] is None
    assert "ignored-top-level-event-content" in normalized["protocolNormalizations"]


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


def test_compound_stop_cannot_suppress_a_reply_bearing_decision():
    for extra in ({"taskControl": "request-help"}, {"intents": [
        {"kind": "foreground-dialogue", "text": "What is nine times nine?", "parameters": {}},
    ]}):
        router = SemanticTurnRouter("test-key", generation_call=lambda **_: provider_response(
            decision(speechControl="stop-speaking", **extra)))
        result = asyncio.run(router.classify("Stop. What is nine times nine?", []))
        assert result["status"] == "classified"
        assert result["speechControl"] is None
        assert "compound-stop-preserved-reply" in result["protocolNormalizations"]


def test_cancellation_after_silence_targets_supplied_question_not_stop_turn():
    payload = {"v": 1, "speechControl": "stop-speaking", "revision": {
        "type": "cancellation", "supersedesTurnId": "question"}}
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return provider_response(json.dumps(payload))

    router = SemanticTurnRouter("test-key", generation_call=generate)
    router.set_turn_context(clock_context=None, memory_draft=None,
                            previous_turn={"turnId": "silence"}, previous_answer={"turnId": "question"})
    result = asyncio.run(router.classify("I no longer need that answer.", []))
    assert result["revision"]["supersedesTurnId"] == "question"
    assert '"previousAnswer": {"turnId": "question"}' in calls[0]["messages"][0]["content"]
    payload["revision"]["supersedesTurnId"] = "unrelated"
    assert asyncio.run(router.classify("I no longer need that answer.", []))["status"] == "invalid"
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


def test_invalid_optional_memory_draft_does_not_discard_valid_skill_control():
    payload = {
        "v": 1, "skill": "field-notes",
        "memoryDraft": {"mode": "start", "subject": None, "predicate": None,
                        "language": "zh", "missingFields": ["subject", "predicate"]},
    }
    router = SemanticTurnRouter("test-key", generation_call=lambda **_: provider_response(json.dumps(payload)))
    catalog = [{"name": "field-notes", "description": "Organize dictated field notes"}]
    result = asyncio.run(router.classify("请整理现场记录：仓库收到六箱配件", catalog))
    assert result["status"] == "classified"
    assert result["selectedSkill"] == "field-notes"
    assert result["memoryDraft"] is None
    assert result["memoryAtoms"] == []
    assert result["memoryKind"] is None
    assert result["componentErrors"][0]["component"] == "memoryDraft"
    payload.pop("skill")
    payload["skillControl"] = "deactivate"
    result = asyncio.run(router.classify("退出当前 Skill", catalog))
    assert result["skillControl"] == "deactivate"
    assert result["memoryDraft"] is None


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
    supplied = json.loads(calls[0]["messages"][0]["content"].split('current transcript): ', 1)[1])
    assert calls[0]["messages"][1]["content"] == "是13659892706"
    assert supplied["clock"]["timezone"] == "Asia/Shanghai"
    assert supplied["pendingMemoryDraft"]["subject"] == "小李"


def test_router_revision_must_target_the_supplied_previous_turn():
    valid = json.loads(decision())
    valid["revision"] = {"type": "correction", "supersedesTurnId": "turn-1"}
    router = SemanticTurnRouter(
        "test-key", generation_call=lambda **_: provider_response(json.dumps(valid)),
    )
    router.set_turn_context(
        clock_context=None, memory_draft=None,
        previous_turn={"turnId": "turn-1", "text": "old request"},
    )
    result = asyncio.run(router.classify("correction", []))
    assert result["revision"] == valid["revision"]

    valid["revision"]["supersedesTurnId"] = "turn-other"
    invalid = asyncio.run(router.classify("bad correction target", []))
    assert invalid["status"] == "invalid"
    assert invalid["revision"] is None


def test_router_turn_context_is_isolated_between_concurrent_classifications():
    supplied = {}

    def generation_call(**kwargs):
        payload = json.loads(kwargs["messages"][0]["content"].split('current transcript): ', 1)[1])
        supplied[kwargs["messages"][1]["content"]] = payload["previousTurn"]["turnId"]
        return provider_response(decision())

    router = SemanticTurnRouter("test-key", generation_call=generation_call)

    async def classify(text, previous_id):
        router.set_turn_context(
            clock_context=None, memory_draft=None,
            previous_turn={"turnId": previous_id},
        )
        await asyncio.sleep(0)
        return await router.classify(text, [])

    async def scenario():
        await asyncio.gather(classify("first", "previous-1"), classify("second", "previous-2"))

    asyncio.run(scenario())
    assert supplied == {"first": "previous-1", "second": "previous-2"}


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


def test_explicit_numeric_memory_does_not_invent_phone_relation():
    for text in ("记住：我的储物柜编号是7429。", "记住：预算是1200元。"):
        for pending in (None, {"predicate": "phone", "subject": None}):
            atoms = explicit_memory_fallback_atoms(text, pending)
            assert atoms[0]["text"] == text
            assert atoms[0]["predicate"] != "phone"


def test_explicit_memory_fallback_associates_number_before_subject():
    atoms = explicit_memory_fallback_atoms(
        "幺二三四五六七，记住这个号码，这是小陈的小号。",
    )
    assert [(item["subject"], item["predicate"], item["value"]) for item in atoms] == [
        ("小陈", "phone", "1234567"),
    ]
