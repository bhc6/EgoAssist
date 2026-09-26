import asyncio
import json
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from egoassist.realtime.decisions_router import DecisionsTurnRouter, create_turn_router
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.prompts import compact_audit_payload, prompt_bundle


def typed_body(questions, **choices):
    answers = {}
    for key, question in questions.items():
        choice = choices.get(key, "none" if "none" in question["criteria"] else next(iter(question["criteria"])))
        answers[key] = {"type": "choice", "choice": choice,
                        "probabilities": {k: float(k == choice) for k in question["criteria"]}}
    return {"model": "resolved-decisions", "id": "request-id", "usage": {"cost": .001}, "answers": answers}


def body(route="reply", retention="none"):
    return typed_body(prompt_bundle("decisions_router")["questions"], route=route, retention=retention)


def test_factory_selects_explicit_backend(monkeypatch):
    monkeypatch.setenv("EGOASSIST_TURN_ROUTER_BACKEND", "openrouter-decisions")
    assert isinstance(create_turn_router("qwen"), DecisionsTurnRouter)
    assert type(create_turn_router("qwen", backend="qwen")) is SemanticTurnRouter
    with pytest.raises(ValueError): create_turn_router("qwen", backend="unknown")


@pytest.mark.parametrize("route,action,output", [("reply", "reply", "speech"), ("stop", "listen", "silent"),
                                                ("other", "listen", "silent"), ("uncertain", "uncertain", "silent"),
                                                ("keep", "keep", "silent"), ("wait", "wait", "silent")])
def test_fast_route_uses_only_decisions_and_normalizes(route, action, output):
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="openrouter")
        async def decide(payload, audit):
            audit["resolvedModel"] = "resolved-decisions"
            return body(route)["answers"]
        router._request_decisions = decide
        router.set_turn_context(clock_context=None, memory_draft=None, output_permission={"paused": True, "quiet": False})
        result = await router.classify("current input", [])
        assert result["status"] == "classified"
        assert result["interaction"]["action"] == action
        assert result["interaction"]["output"] == output
        assert result["model"] == "resolved-decisions"
        assert result["modelsUsed"] == ["resolved-decisions"]
        assert not result["memoryAtoms"] and not result["intents"]
        assert "messages" not in result["requestMetrics"]
        await router.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["structured", "draft", "explicit-reply"])
def test_structured_work_preserves_existing_contract(reason):
    async def run():
        calls = []
        async def extract(**kwargs):
            calls.append(kwargs)
            wire = {"v": 1, "memoryEvents": [{"scope": "participant", "consent": "explicit"}],
                    "interaction": {"audience": "assistant", "action": "reply", "output": "speech"}}
            return SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(wire)))]))
        router = DecisionsTurnRouter("qwen", decisions_key="openrouter", generation_call=extract)
        async def decide(payload, audit):
            return body("structured" if reason == "structured" else "reply", retention="explicit")["answers"]
        router._request_decisions = decide
        router.set_turn_context(clock_context=None,
            memory_draft={"mode": "start"} if reason == "draft" else None,
            active_task={"paused": True} if reason == "task" else None,
            output_permission={"quiet": True} if reason == "quiet" else None)
        result = await router.classify("记住我的柜子是三号", [])
        assert len(calls) == 1
        assert result["status"] == "classified"
        assert result["memoryAtoms"][0]["consent"] == "explicit"
        assert result["memoryAtoms"][0]["text"] == "记住我的柜子是三号"
        assert result["requestMetrics"]["structuredReason"]
        assert result["model"] == router.model
        await router.aclose()
    asyncio.run(run())


def test_async_transport_cancellation_and_error_isolation(monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        status = 200
        requests = []
        async def handler(request):
            data = await request.json()
            requests.append(data)
            assert request.headers["Authorization"] == "Bearer openrouter-secret"
            if json.loads(data["state"])["currentUtterance"] == "slow":
                entered.set()
                await release.wait()
            return web.json_response(body(), status=status)
        app = web.Application()
        app.router.add_post("/decisions", handler)
        async with TestServer(app) as server:
            monkeypatch.setattr("egoassist.realtime.decisions_router.DECISIONS_URL", str(server.make_url("/decisions")))
            router = DecisionsTurnRouter("qwen-secret", decisions_key="openrouter-secret", timeout_seconds=.5, decisions_timeout=.1)
            try:
                result = await router.classify("slow", [])
                assert result["status"] == "unavailable" and result["timedOut"]
                assert not router._decisions_session.connector._acquired
                entered.clear()
                pending = asyncio.create_task(router.classify("slow", []))
                await entered.wait()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError): await pending
                assert not router._decisions_session.connector._acquired
                status = 401
                result = await router.classify("fast", [])
                assert result["status"] == "unavailable" and not result["retryable"]
                count = len(requests)
                await router.classify("fast", [])
                assert len(requests) == count
                status = 200
                router.reset_failure()
                result = await router.classify("fast", [])
                assert result["status"] == "classified"
                assert "secret" not in json.dumps(result)
                assert router._http_session is None  # no hidden Qwen retry
            finally:
                release.set()
                session = router._decisions_session
                await router.aclose()
                assert session.closed
    asyncio.run(run())


def test_invalid_response_never_falls_through_to_actions(monkeypatch):
    async def run():
        async def handler(request):
            return web.json_response({"answers": {}})
        app = web.Application()
        app.router.add_post("/decisions", handler)
        async with TestServer(app) as server:
            monkeypatch.setattr("egoassist.realtime.decisions_router.DECISIONS_URL", str(server.make_url("/decisions")))
            router = DecisionsTurnRouter("qwen", decisions_key="openrouter")
            try:
                result = await router.classify("stop", [])
                assert result["status"] == "invalid"
                assert result["interaction"]["action"] == "uncertain"
                assert result["speechControl"] is None and result["taskControl"] is None
                assert router._http_session is None
            finally: await router.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["cold-success", "connect-stalled", "read-stalled", "total-deadline"])
def test_cold_connection_preserves_response_budget_and_total_deadline(monkeypatch, case):
    async def run():
        calls = []
        # The connection and response each fit the stage budget, but their
        # sum exceeds it. Previously cold-success timed out before the answer.
        class SlowConnector(aiohttp.TCPConnector):
            async def _create_connection(self, *args, **kwargs):
                await asyncio.sleep(.6 if case == "connect-stalled" else .16)
                return await super()._create_connection(*args, **kwargs)

        async def handler(request):
            calls.append(await request.json())
            await asyncio.sleep(.6 if case == "read-stalled" else .16)
            return web.json_response(body())

        app = web.Application()
        app.router.add_post("/decisions", handler)
        async with TestServer(app) as server:
            monkeypatch.setattr("egoassist.realtime.decisions_router.DECISIONS_URL", str(server.make_url("/decisions")))
            async with aiohttp.ClientSession(connector=SlowConnector()) as session:
                router = DecisionsTurnRouter("qwen", decisions_key="test", decisions_session=session,
                    timeout_seconds=.28 if case == "total-deadline" else .8, decisions_timeout=.25)
                try:
                    async with asyncio.timeout(1):
                        result = await router.classify("hello", [])
                    if case == "cold-success":
                        assert result["status"] == "classified"
                        assert result["interaction"]["action"] == "reply"
                        assert len(calls) == 1
                    else:
                        assert result["status"] == "unavailable" and result["timedOut"]
                        assert result["interaction"]["action"] == "uncertain"
                    assert not session.connector._acquired
                    assert router._http_session is None
                finally:
                    await router.aclose()
                assert not session.closed  # Shared transport ownership is unchanged.
    asyncio.run(run())


def test_missing_key_is_explicit_provider_failure():
    result = asyncio.run(DecisionsTurnRouter("qwen", decisions_key="").classify("hello", []))
    assert result["status"] == "unavailable"
    assert result["providerError"]["code"] == "MissingOpenRouterKey"


@pytest.mark.parametrize("retention", ["source", "none", "forbid", "mixed", "draft"])
def test_retention_ceiling_cannot_promote_extracted_facts(retention):
    async def run():
        async def extract(**kwargs):
            wire = {"v": 1, "memoryAtoms": [{"kind": "fact", "scope": "participant", "consent": "explicit",
                    "subject": "number", "predicate": "value", "value": "7316", "text": "number 7316"}],
                    "preferenceIntent": "propose-durable"}
            return SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(wire)))]))
        router = DecisionsTurnRouter("qwen", decisions_key="openrouter", generation_call=extract)
        async def decide(payload, audit): return body("structured", retention=retention)["answers"]
        router._request_decisions = decide
        result = await router.classify("number 7316", [])
        assert result["status"] == "classified"
        assert not result["componentErrors"]
        assert result["preferenceIntent"] is None
        if retention == "source":
            assert len(result["memoryAtoms"]) == 1
            assert result["memoryAtoms"][0]["kind"] == "event"
            assert result["memoryAtoms"][0]["consent"] == "observed"
            assert result["memoryAtoms"][0]["scope"] == "case"
        else:
            assert not result["memoryAtoms"] and result["memoryKind"] is None
        assert result["requestMetrics"]["decisions"]["structuredRawOutput"]
        await router.aclose()
    asyncio.run(run())


def test_polling_hides_full_decisions_input_without_mutating_audit():
    original = {"decision": {"requestMetrics": {"decisions": {"request": {"state": "large"},
                "rawResponse": "large", "answers": {"route": {}}, "model": "jev"}}}}
    compact = compact_audit_payload("control_decision", original)
    audit = compact["decision"]["requestMetrics"]["decisions"]
    assert "request" not in audit and "rawResponse" not in audit
    assert audit["fullRequestAvailable"]
    assert original["decision"]["requestMetrics"]["decisions"]["request"]["state"] == "large"


def test_combined_deadline_cancels_structured_extraction():
    async def run():
        cancelled = asyncio.Event()
        async def extract(**kwargs):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        router = DecisionsTurnRouter("qwen", decisions_key="openrouter", generation_call=extract,
                                     timeout_seconds=.05, decisions_timeout=.04)
        async def decide(payload, audit): return body("structured")["answers"]
        router._request_decisions = decide
        result = await router.classify("hello", [])
        assert result["status"] == "unavailable" and result["timedOut"]
        assert cancelled.is_set()
        await router.aclose()
    asyncio.run(run())


def test_concurrent_context_is_not_shared():
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="openrouter")
        received = []
        async def decide(payload, audit):
            received.append(json.loads(payload["state"]))
            await asyncio.sleep(.01)
            return body()["answers"]
        router._request_decisions = decide
        async def turn(text):
            router.set_turn_context(clock_context=None, memory_draft=None, previous_turn={"turnId": text, "text": text})
            return await router.classify(text, [])
        results = await asyncio.gather(turn("first"), turn("second"))
        assert all(r["status"] == "classified" for r in results)
        assert all(s["currentUtterance"] == s["context"]["previousTurn"]["turnId"] for s in received)
        await router.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("retention,scope,consent", [("explicit", "participant", "explicit"), ("source", "case", "observed")])
def test_source_memory_uses_literal_input_without_extraction(retention, scope, consent):
    async def run():
        async def forbidden(**kwargs): raise AssertionError("Qwen must not be called")
        router = DecisionsTurnRouter("qwen", decisions_key="jev", generation_call=forbidden)
        async def decide(payload, audit): return typed_body(payload["questions"], route="remember", retention=retention)["answers"]
        router._request_decisions = decide
        result = await router.classify("我的柜子编号是7429。", [])
        assert result["status"] == "classified"
        assert len(result["memoryAtoms"]) == 1
        atom = result["memoryAtoms"][0]
        assert (atom["scope"], atom["consent"], atom["text"]) == (scope, consent, "我的柜子编号是7429。")
        assert not result["requestMetrics"].get("structuredReason")
        assert atom["subject"] is None  # no invented entity extraction
    asyncio.run(run())


def test_simple_recall_preserves_query_and_quiet_permission():
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="jev")
        router.set_turn_context(clock_context=None, memory_draft=None, output_permission={"quiet": True, "allowReplies": False})
        async def decide(payload, audit): return typed_body(payload["questions"], route="recall")["answers"]
        router._request_decisions = decide
        result = await router.classify("我的柜子编号是什么？", [])
        assert result["status"] == "classified"
        assert result["memoryQuery"] == "referential"
        assert result["intents"][0]["text"] == "我的柜子编号是什么？"
        assert "quiet" not in result["interaction"]  # grants remain checked by executor
        assert router._http_session is None
    asyncio.run(run())


def test_task_confirmation_and_independent_review_use_jev_only():
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="jev")
        router.set_turn_context(clock_context=None, memory_draft=None, active_task={"currentStep": {"completion": "all ready"}})
        calls = []
        async def decide(payload, audit):
            calls.append(payload)
            return typed_body(payload["questions"], route="task", taskControl="confirm-step", verdict="confirm-step")["answers"]
        router._request_decisions = decide
        result = await router.classify("all ready", [])
        assert result["status"] == "classified" and result["taskControl"] == "confirm-step"
        assert len(calls) == 2 and "verdict" in calls[1]["questions"]
        assert router._http_session is None
    asyncio.run(run())


def test_task_review_failure_does_not_confirm_step():
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="jev")
        router.set_turn_context(clock_context=None, memory_draft=None, active_task={"currentStep": {"completion": "all ready"}})
        async def decide(payload, audit):
            if "verdict" in payload["questions"]: raise TimeoutError()
            return typed_body(payload["questions"], route="task", taskControl="confirm-step")["answers"]
        router._request_decisions = decide
        result = await router.classify("all ready", [])
        assert result["status"] == "unavailable" and result["taskControl"] is None
    asyncio.run(run())


@pytest.mark.parametrize("verdict,expected", [("yes", "cancel"), ("no", "uncertain")])
def test_jev_cancellation_targets_supplied_older_answer_and_is_reviewed(verdict, expected):
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="jev")
        router.set_turn_context(clock_context=None, memory_draft=None,
            answers=[{"turnId": "old", "text": "multiplication", "status": "running"},
                     {"turnId": "new", "text": "plants", "status": "running"}])
        async def decide(payload, audit): return typed_body(payload["questions"], route="cancel", target="t1", verdict=verdict)["answers"]
        router._request_decisions = decide
        result = await router.classify("cancel multiplication", [])
        assert result["status"] == "classified"
        assert result["interaction"]["action"] == expected
        if verdict == "yes": assert result["interaction"]["targetTurnId"] == "old"
        else: assert "targetTurnId" not in result["interaction"]
        assert router._http_session is None
    asyncio.run(run())


def test_missing_target_never_cancels_most_recent_answer():
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="jev")
        router.set_turn_context(clock_context=None, memory_draft=None, answers=[{"turnId": "known", "text": "plants"}])
        async def decide(payload, audit): return typed_body(payload["questions"], route="cancel", target="none")["answers"]
        router._request_decisions = decide
        result = await router.classify("cancel an absent question", [])
        assert result["interaction"]["action"] == "uncertain"
        assert "targetTurnId" not in result["interaction"]
    asyncio.run(run())


def test_skill_alias_maps_to_scoped_catalog_without_prompt_mutation():
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="jev")
        original = json.dumps(prompt_bundle("decisions_router"), sort_keys=True)
        async def decide(payload, audit): return typed_body(payload["questions"], route="reply", skill="s1")["answers"]
        router._request_decisions = decide
        result = await router.classify("start the supplied workflow", [{"name": "supplied-skill", "description": "workflow"}])
        assert result["selectedSkill"] == "supplied-skill"
        assert json.dumps(prompt_bundle("decisions_router"), sort_keys=True) == original
        assert router._http_session is None
    asyncio.run(run())


def test_uncertain_details_delegate_without_losing_source_time_context():
    async def run():
        context = {"times": [{"relation": "放", "expression": "昨天", "anchor": "turn"}]}
        async def extract(**kwargs):
            wire = {"v": 1, "memoryEvents": [{"scope": "case", "consent": "observed", "context": context}]}
            return SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(wire)))]))
        router = DecisionsTurnRouter("qwen", decisions_key="jev", generation_call=extract)
        async def decide(payload, audit):
            answers = typed_body(payload["questions"], route="remember", retention="source")["answers"]
            answers["details"]["probabilities"] = {"none": .65, "extract": .35}
            return answers
        router._request_decisions = decide
        result = await router.classify("昨天我放了钥匙", [])
        assert result["status"] == "classified"
        assert result["requestMetrics"]["structuredReason"] == "ambiguous-details"
        assert result["memoryAtoms"][0]["context"]["times"] == context["times"]
    asyncio.run(run())


def test_answer_target_payload_excludes_control_only_turn_and_maps_completed_answer():
    async def run():
        router = DecisionsTurnRouter("qwen", decisions_key="jev")
        router.set_turn_context(clock_context=None, memory_draft=None,
            previous_turn={"turnId": "control", "text": "only show it", "status": "completed"},
            previous_answer={"turnId": "answer", "text": "explain multiplication", "status": "completed"},
            answers=[{"turnId": "answer", "text": "explain multiplication", "status": "completed"},
                     {"turnId": "cancelled", "text": "old question", "status": "cancelled"}],
            playback={"turnId": "answer"})
        async def decide(payload, audit):
            targets = json.loads(payload["state"])["answerTargets"]
            assert targets == [{"choice": "t1", "turnId": "answer", "question": "explain multiplication",
                                "status": "completed", "previousAnswer": True, "playingAnswer": True}]
            assert set(payload["questions"]["target"]["criteria"]) == {"none", "t1"}
            return typed_body(payload["questions"], route="resume", target="t1")["answers"]
        router._request_decisions = decide
        result = await router.classify("read that answer again", [])
        assert result["interaction"]["action"] == "resume"
        assert result["interaction"]["targetTurnId"] == "answer"
        assert not result["requestMetrics"].get("structuredReason")
    asyncio.run(run())


def test_application_pool_reuses_transport_without_sharing_keys_cookies_or_circuits(monkeypatch):
    from egoassist.realtime.decisions_router import DECISIONS_SESSION, decisions_http_context
    from aiohttp.test_utils import TestClient

    async def run():
        warmed = asyncio.Event()
        warm_auth, received = [], []

        async def warm(request):
            warm_auth.append(request.headers.get("Authorization"))
            warmed.set()
            response = web.json_response({"count": 1})
            response.set_cookie("account", "must-not-be-shared")
            return response

        async def classify(request):
            assert "Cookie" not in request.headers
            payload = await request.json()
            auth = request.headers.get("Authorization")
            received.append(auth)
            if auth == "Bearer blocked":
                return web.Response(status=401)
            return web.json_response(typed_body(payload["questions"], route="reply"))

        remote = web.Application()
        remote.router.add_get("/count", warm)
        remote.router.add_post("/decisions", classify)
        async with TestServer(remote) as server:
            monkeypatch.setenv("EGOASSIST_TURN_ROUTER_BACKEND", "openrouter-decisions")
            monkeypatch.setenv("EGOASSIST_DECISIONS_CONNECTION_POOL", "1")
            monkeypatch.setenv("EGOASSIST_DECISIONS_PRECONNECT", "1")
            monkeypatch.setattr("egoassist.realtime.decisions_router.PRECONNECT_URL", str(server.make_url("/count")))
            monkeypatch.setattr("egoassist.realtime.decisions_router.DECISIONS_URL", str(server.make_url("/decisions")))
            app = web.Application()
            app.cleanup_ctx.append(decisions_http_context)
            async with TestClient(TestServer(app)):
                await asyncio.wait_for(warmed.wait(), 2)
                await asyncio.sleep(.01)
                session = app[DECISIONS_SESSION]
                first = DecisionsTurnRouter("qwen", decisions_key="blocked", decisions_session=session)
                second = DecisionsTurnRouter("qwen", decisions_key="valid", decisions_session=session)
                try:
                    assert (await first.classify("first", []))["status"] == "unavailable"
                    decision = await second.classify("second", [])
                    assert decision["status"] == "classified"
                    assert decision["requestMetrics"]["decisions"]["transport"]["connectionReused"]
                    await first.aclose()
                    assert not session.closed
                    assert (await second.classify("third", []))["status"] == "classified"
                    assert received == ["Bearer blocked", "Bearer valid", "Bearer valid"]
                    assert warm_auth == [None]
                finally:
                    await first.aclose()
                    await second.aclose()
                assert not session.closed
            assert session.closed
    asyncio.run(run())
