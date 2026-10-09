"""Typed front router with the existing validated protocol for structured work."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import re
import time

import aiohttp
from aiohttp import web

from egoassist.core.provider_errors import ProviderRequestError
from egoassist.core.typed_decisions import validate_answers
from egoassist.prompts import prompt_bundle
from .router_context import project_context
from .semantic import SemanticTurnRouter
from .router_protocol import expand_decision


DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
PRECONNECT_URL = "https://openrouter.ai/api/v1/models/count"
DECISIONS_SESSION = web.AppKey("egoassist.decisions.http", aiohttp.ClientSession)

_TEMPORAL_RECALL = re.compile(r"(?:今天|今日|昨天|昨晚|今早|今晨|这周|本周|最近|刚才|之前)")
_COUNT_QUESTION = re.compile(r"(?:几次|多少次|次数|几回|多少回|几遍|多少遍|有没有|是否|吗[？?！!。]?$)")


def looks_like_temporal_recall(text: str) -> bool:
    """Keep explicit period/count questions on the recall path.

    This is a routing floor only. It does not extract facts or answer the
    question; it prevents a source statement or current visual frame from
    being mistaken for a complete historical count.
    """
    value = "".join(str(text or "").split())
    if not value or not _TEMPORAL_RECALL.search(value):
        return False
    return bool(_COUNT_QUESTION.search(value) or re.search(r"(?:几|多少).{0,6}(?:喝|吃|看|做|次|个|件)", value))


def transport_trace():
    """Record per-request timings without URLs, headers or cross-turn state."""
    trace = aiohttp.TraceConfig()

    async def start(session, ctx, params):
        ctx.started = time.perf_counter()
        ctx.metrics = ctx.trace_request_ctx if ctx.trace_request_ctx is not None else {}
        ctx.metrics["connectionReused"] = False

    async def connect_start(session, ctx, params):
        ctx.connect_started = time.perf_counter()

    async def connect_end(session, ctx, params):
        ctx.metrics["connectMs"] = round((time.perf_counter() - ctx.connect_started) * 1000, 1)

    async def reused(session, ctx, params):
        ctx.metrics["connectionReused"] = True

    async def dns_start(session, ctx, params):
        ctx.dns_started = time.perf_counter()

    async def dns_end(session, ctx, params):
        ctx.metrics["dnsMs"] = round((time.perf_counter() - ctx.dns_started) * 1000, 1)

    async def headers_sent(session, ctx, params):
        ctx.metrics["headersSentMs"] = round((time.perf_counter() - ctx.started) * 1000, 1)

    async def end(session, ctx, params):
        ctx.metrics["responseHeadersMs"] = round((time.perf_counter() - ctx.started) * 1000, 1)

    for signal, callback in [(trace.on_request_start, start), (trace.on_connection_create_start, connect_start),
            (trace.on_connection_create_end, connect_end), (trace.on_connection_reuseconn, reused),
            (trace.on_dns_resolvehost_start, dns_start), (trace.on_dns_resolvehost_end, dns_end),
            (trace.on_request_headers_sent, headers_sent), (trace.on_request_end, end)]:
        signal.append(callback)
    return trace


async def decisions_http_context(app):
    """App-owned, cookie-free transport. Router state stays connection-local."""
    from egoassist.core.settings import integer
    if (os.getenv("EGOASSIST_TURN_ROUTER_BACKEND", "qwen") != "openrouter-decisions"
            or not integer("EGOASSIST_DECISIONS_CONNECTION_POOL", 1, minimum=0, maximum=1)):
        yield
        return
    count = integer("EGOASSIST_DECISIONS_PRECONNECT", 4, minimum=0, maximum=8)
    async with aiohttp.ClientSession(trust_env=True, cookie_jar=aiohttp.DummyCookieJar(),
            connector=aiohttp.TCPConnector(keepalive_timeout=60, limit=32, limit_per_host=32),
            trace_configs=[transport_trace()]) as session:
        app[DECISIONS_SESSION] = session

        async def warm():
            # No credentials, user text or model invocation. A failed warm-up
            # never blocks startup and is not mistaken for provider health.
            try:
                async with session.get(PRECONNECT_URL, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=1.5)) as response:
                    total = 0
                    async for chunk in response.content.iter_chunked(4096):
                        total += len(chunk)
                        if total > 65536:
                            break
            except (aiohttp.ClientError, TimeoutError):
                pass

        tasks = [asyncio.create_task(warm(), name="jev-preconnect") for _ in range(count)]
        try:
            yield
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class DecisionsTurnRouter(SemanticTurnRouter):
    """JEV handles finite decisions; Qwen extracts non-enumerable content.

    `model` remains the structured model used by inherited review methods.
    Decisions transport uses its own key, session, model and circuit state.
    All outputs still pass SemanticTurnRouter's normalization and checks.
    """

    def __init__(self, api_key, *, decisions_key=None, decisions_model=None,
                 decisions_timeout=None, decisions_session=None, **kwargs):
        super().__init__(api_key, **kwargs)
        self.decisions_key = (decisions_key if decisions_key is not None else os.getenv("OPENROUTER_API_KEY", "")).strip()
        self.decisions_model = decisions_model or os.getenv("EGOASSIST_DECISIONS_MODEL", "typesafe/jev-1.13")
        self.decisions_timeout = float(decisions_timeout if decisions_timeout is not None else
                                      os.getenv("EGOASSIST_DECISIONS_TIMEOUT", "2.0"))
        if not 0 < self.decisions_timeout <= self.timeout_seconds:
            raise ValueError("Decisions timeout must be positive and within the router deadline")
        self._decisions_session = decisions_session
        self._owns_decisions_session = decisions_session is None
        self._decisions_failure = None

    def reset_failure(self):
        super().reset_failure()
        self._decisions_failure = None

    async def aclose(self):
        try:
            if self._decisions_session is not None:
                if self._owns_decisions_session:
                    await self._decisions_session.close()
                self._decisions_session = None
        finally:
            await super().aclose()

    async def classify(self, text, skill_catalog):
        result = await super().classify(text, skill_catalog)
        metrics = result["requestMetrics"]
        result["routerBackend"] = "openrouter-decisions"
        result["policyVersion"] = "decisions-first-v8/" + result["policyVersion"]
        result["method"] = result["policyVersion"]
        result["modelsUsed"] = []
        if metrics.get("decisions"):
            result["modelsUsed"].append(metrics["decisions"].get("resolvedModel", self.decisions_model))
        if metrics.get("structuredReason"):
            result["modelsUsed"].append(self.model)
        if not metrics.get("structuredReason"):
            result["model"] = (metrics.get("decisions") or {}).get("resolvedModel", self.decisions_model)
        result["evidenceScope"] = (metrics.get("decisions") or {}).get("evidenceScope", "none")
        return result

    @staticmethod
    def _questions(bundle, context, skill_catalog):
        questions = deepcopy(bundle["questions"])
        skill_ids, target_ids, targets = {}, {}, {}
        if context.get("activeTask"):
            questions["taskControl"] = deepcopy(bundle["taskQuestion"])
        if skill_catalog:
            question = questions["skill"] = deepcopy(bundle["skillQuestion"])
            for index, item in enumerate(skill_catalog):
                alias = f"s{index + 1}"
                skill_ids[alias] = item["name"]
                question["criteria"][alias] = json.dumps(item, ensure_ascii=False)
        # A previous turn may be a control command without an answer. Only
        # actual answer candidates may become pause/cancel/resume targets.
        for row in [*context.get("answers", []), context.get("previousAnswer") or {}]:
            if row.get("turnId") and row.get("text") and row.get("status") != "cancelled":
                targets.setdefault(row["turnId"], row)
        if targets:
            question = questions["target"] = deepcopy(bundle["targetQuestion"])
            for index, (turn_id, row) in enumerate(targets.items()):
                alias = f"t{index + 1}"
                target_ids[alias] = turn_id
                question["criteria"][alias] = json.dumps({"question": row["text"], "status": row.get("status"),
                    "previousAnswer": turn_id == (context.get("previousAnswer") or {}).get("turnId"),
                    "playingAnswer": turn_id == (context.get("playback") or {}).get("turnId")}, ensure_ascii=False)
        return questions, skill_ids, target_ids

    async def _classify_request(self, text, skill_catalog, metrics):
        bundle = prompt_bundle("decisions_router")
        context = self._turn_context.get()
        questions, skill_ids, target_ids = self._questions(bundle, context, skill_catalog)
        state = {"currentUtterance": text, "context": project_context(context),
                 "skills": [{"name": x["name"], "description": x["description"]} for x in skill_catalog]}
        if target_ids:
            state["answerTargets"] = [{"choice": alias, "turnId": turn_id,
                **json.loads(questions["target"]["criteria"][alias])} for alias, turn_id in target_ids.items()]
        payload = {"model": self.decisions_model, "state": json.dumps(state, ensure_ascii=False), "questions": questions}
        audit = metrics["decisions"] = {"endpoint": DECISIONS_URL, "model": self.decisions_model,
                                        "promptVersion": bundle["version"], "request": payload}
        answers = await self._request_decisions(payload, audit)
        choices = {key: value["choice"] for key, value in answers.items()}
        route, retention = choices["route"], choices["retention"]
        evidence_scope = choices.get("evidenceScope", "none")
        explicit_temporal_question = looks_like_temporal_recall(text)
        memory_write = route == "remember" and retention in {"explicit", "source", "draft"}
        temporal_recall = explicit_temporal_question or (evidence_scope == "temporal-history" and not memory_write)
        audit["evidenceScope"] = evidence_scope
        if (evidence_scope in {"temporal-history", "stored-memory"} and not memory_write
                and route in {"reply", "text", "remember", "structured"}):
            audit["modelRoute"] = route
            audit["modelRetention"] = retention
            audit["routeOverride"] = "evidence-scope-recall"
            route, retention = "recall", "none"
        if route in {"remember", "reply", "text", "task", "structured"} and temporal_recall:
            audit["modelRoute"] = route
            audit["modelRetention"] = retention
            audit["routeOverride"] = "temporal-count-recall"
            route, retention = "recall", "none"
        audit["route"] = route
        reason = None
        if context.get("memoryDraft") or retention == "draft":
            reason = "pending-memory-draft"
        elif evidence_scope == "temporal-history" and temporal_recall:
            reason = "temporal-history-evidence"
        elif route == "structured" or retention == "mixed" or choices["details"] == "extract":
            reason = "structured-content"
        elif answers["details"]["probabilities"]["extract"] >= 0.3:
            # A development heuristic to request MORE parsing when details may
            # be missing. This is not calibrated evidence or action authority.
            reason = "ambiguous-details"
        elif retention == "explicit" and route != "remember":
            reason = "structured-explicit-retention"
        elif route == "remember" and retention not in {"explicit", "source"}:
            reason = "inconsistent-memory-decisions"
        elif retention == "source" and route not in {"remember", "reply", "text", "task"}:
            reason = "source-and-control"
        elif choices.get("taskControl", "none") != "none" and route not in {"reply", "text", "task"}:
            reason = "compound-task-control"
        if reason:
            metrics["structuredReason"] = reason
            raw = await super()._classify_request(text, skill_catalog, metrics)
            audit["structuredRawOutput"] = raw
            parsed = expand_decision(json.loads(raw))
            self._restrict_retention(parsed, retention, context, audit)
            if temporal_recall:
                # The count question is a historical recall even when the
                # second-stage extractor reports ambiguous details. Keep it
                # out of source-memory writes and out of current-frame answers.
                parsed["memoryQuery"] = "referential"
                parsed["memoryQuerySpec"] = None
                parsed["memoryAtoms"] = []
                parsed["memoryDraft"] = None
                parsed["preferenceIntent"] = "none"
                parsed["intents"] = [{"kind": "foreground-dialogue", "text": text, "parameters": {}}]
                parsed["interaction"] = {"audience": "assistant", "action": "reply", "output": "speech"}
                audit["structuredRouteOverride"] = "temporal-count-recall"
            return json.dumps(parsed, ensure_ascii=False)

        interaction = {
            "reply": {"audience": "assistant", "action": "reply", "output": "speech"},
            "text": {"audience": "assistant", "action": "reply", "output": "text"},
            "remember": {"audience": "assistant", "action": "reply", "output": "speech"},
            "recall": {"audience": "assistant", "action": "reply", "output": "speech"},
            "task": {"audience": "assistant", "action": "reply", "output": "speech"},
            "stop": {"audience": "assistant", "action": "listen", "output": "silent"},
            "other": {"audience": "other", "action": "listen", "output": "silent"},
            "keep": {"audience": "assistant", "action": "keep", "output": "silent"},
            "wait": {"audience": "assistant", "action": "wait", "output": "silent"},
            "uncertain": {"audience": "uncertain", "action": "uncertain", "output": "silent"},
            "resume": {"audience": "assistant", "action": "resume", "output": "speech"},
            "resume-text": {"audience": "assistant", "action": "resume", "output": "text"},
            "cancel": {"audience": "assistant", "action": "cancel", "output": "silent"},
            "pause": {"audience": "assistant", "action": "pause", "output": "silent"},
        }[route]
        if route in {"resume", "resume-text", "cancel", "pause"}:
            target = target_ids.get(choices.get("target"))
            if target:
                interaction["targetTurnId"] = target
            elif route in {"resume", "resume-text"} and not target_ids:
                # No existing answer to resume: let the reply ask for context,
                # rather than inventing an ID or silently swallowing the input.
                interaction["action"] = "reply"
                audit["resumeNeedsContext"] = True
            else:
                interaction.update(action="uncertain", output="silent")
                audit["targetUnavailable"] = True
        quiet = choices["quiet"]
        if quiet != "none" and interaction["audience"] == "assistant" and interaction["action"] != "uncertain":
            interaction["quiet"] = {"mode": "clear" if quiet == "clear" else "set", "allowReplies": quiet != "set-all"}
        parsed = {"v": 1, "interaction": interaction}
        if route in {"reply", "text", "task", "remember"}:
            if retention in {"explicit", "source"}:
                parsed["memoryEvents"] = [{"scope": "participant" if retention == "explicit" else "case",
                                           "consent": "explicit" if retention == "explicit" else "observed"}]
            task_control = choices.get("taskControl", "none")
            if task_control != "none" and (context.get("activeTask") or {}).get("currentStep"):
                parsed["taskControl"] = task_control
            skill = choices.get("skill", "none")
            if skill == "deactivate":
                parsed["skillControl"] = "deactivate"
            elif skill == "ambiguous":
                parsed["skill"] = "ambiguous"
            elif skill in skill_ids:
                parsed["skill"] = skill_ids[skill]
        if route == "recall":
            parsed["memoryQuery"] = "referential"
            parsed["intents"] = [{"kind": "foreground-dialogue", "text": text, "parameters": {}}]
        return json.dumps(parsed, ensure_ascii=False)

    async def _review(self, name, question_key, state, metrics):
        bundle = prompt_bundle("decisions_router")
        payload = {"model": self.decisions_model, "state": json.dumps(state, ensure_ascii=False),
                   "questions": {"verdict": bundle[question_key]}}
        audit = metrics[name] = {"endpoint": DECISIONS_URL, "model": self.decisions_model,
                                 "request": payload, "promptVersion": bundle["version"]}
        try:
            answers = await self._request_decisions(payload, audit)
            return answers["verdict"]["choice"]
        except Exception as exc:
            audit["error"] = type(exc).__name__
            raise

    async def _verify_task_control(self, text, parsed, metrics):
        verdict = await self._review("taskControlCheck", "taskReview", {
            "currentUtterance": text, "activeTask": self._turn_context.get().get("activeTask"),
            "proposedTaskControl": parsed.get("taskControl"), "proposedInteraction": parsed.get("interaction"),
        }, metrics)
        # Unlike the legacy reviewer, do not retain a proposed state change on
        # review failure. All outputs still go through the inherited validator.
        if verdict is None:
            raise RuntimeError("Decisions task review unavailable")
        return {"taskControl": verdict, "taskDirected": verdict in {"pause-task", "resume-task"}}

    async def _verify_cancel(self, text, target, metrics):
        context = self._turn_context.get()
        rows = [*context.get("answers", []), context.get("previousAnswer") or {}, context.get("previousTurn") or {}]
        question = next((r.get("text") for r in rows if r.get("turnId") == target), None)
        if not question:
            return False
        verdict = await self._review("cancellationCheck", "cancelReview", {
            "currentUtterance": text, "targetQuestion": question, "targetTurnId": target,
        }, metrics)
        return verdict == "yes"

    @staticmethod
    def _restrict_retention(parsed, retention, context, audit):
        """A restrictive ceiling, never permission to invent or promote an atom."""
        if retention == "explicit" or (retention == "draft" and context.get("memoryDraft")):
            return
        audit["retentionCeiling"] = retention
        atoms = parsed.get("memoryAtoms")
        # Source preservation avoids a free-text extractor turning casual claims
        # into confirmed participant facts. Keep it scoped and let normalizers
        # attach the complete current source rather than reconstructing a claim.
        if retention == "source" and isinstance(atoms, list) and atoms:
            source_event = {"kind": "event", "scope": "case", "consent": "observed"}
            if len(atoms) == 1 and isinstance(atoms[0], dict) and atoms[0].get("context"):
                source_event["context"] = atoms[0]["context"]
            parsed["memoryAtoms"] = [source_event]
        else:
            parsed["memoryAtoms"] = []
        parsed["memoryDraft"] = None
        parsed["preferenceIntent"] = "none"
        for key in ("memoryKind", "memoryText", "memoryKey", "memoryScope", "memoryConsent"):
            parsed[key] = "none"

    async def _request_decisions(self, payload, audit):
        started = time.perf_counter()
        deadline = self._deadline.get() or (asyncio.get_running_loop().time() + self.timeout_seconds)
        attempts = 0
        retry_transport = False
        try:
            if self._decisions_failure:
                audit["circuitOpen"] = True
                raise self._decisions_failure
            if not self.decisions_key:
                raise ProviderRequestError(401, "MissingOpenRouterKey")
            if self._decisions_session is None:
                self._decisions_session = aiohttp.ClientSession(trust_env=True, trace_configs=[transport_trace()])
            # A cold DNS/TCP/TLS connection must not consume the response-read
            # allowance. The complete classification (including any structured
            # extraction) remains bounded by SemanticTurnRouter's deadline.
            audit["timeoutPolicy"] = {"connectSeconds": self.decisions_timeout,
                                      "readSeconds": self.decisions_timeout,
                                      "totalSeconds": self.timeout_seconds}
            while True:
                attempts += 1
                audit["attempts"] = attempts
                attempt = {"attempt": attempts, "transport": {}}
                audit.setdefault("attemptDetails", []).append(attempt)
                audit["transport"] = attempt["transport"]
                attempt_started = asyncio.get_running_loop().time()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Decisions request deadline exceeded")
                stage_timeout = min(self.decisions_timeout, remaining)
                retry_session = None
                request_session = self._decisions_session
                if attempts > 1 and retry_transport:
                    # A read timeout can leave a pooled connection half-open.
                    # Use a fresh, cookie-free transport for the recovery call.
                    retry_session = aiohttp.ClientSession(trust_env=True, trace_configs=[transport_trace()])
                    request_session = retry_session
                try:
                    async with asyncio.timeout(remaining):
                        async with request_session.post(
                            DECISIONS_URL, json=payload, allow_redirects=False,
                            headers={"Authorization": "Bearer " + self.decisions_key},
                            trace_request_ctx=attempt["transport"],
                            timeout=aiohttp.ClientTimeout(total=None, connect=stage_timeout,
                                                          sock_read=stage_timeout),
                        ) as response:
                            audit["httpStatus"] = response.status
                            attempt["httpStatus"] = response.status
                            raw = (await response.text()).replace(self.decisions_key, "[REDACTED]")
                            audit["rawResponse"] = raw
                            if response.status != 200:
                                raise ProviderRequestError(response.status, "OpenRouterDecisions")
                            body = json.loads(raw)
                            answers = validate_answers(body, payload["questions"])
                            audit.update(requestId=body.get("id"), resolvedModel=body.get("model"),
                                         provider=body.get("provider"), usage=body.get("usage"), answers=answers)
                            audit["attempts"] = attempts
                            return answers
                except ProviderRequestError as exc:
                    attempt["error"] = exc.public()
                    if not (exc.retryable and attempts == 1 and
                            deadline - asyncio.get_running_loop().time() > 0):
                        raise
                    retry_transport = False
                    audit["retry"] = {"attempt": 2, "reason": exc.public()}
                except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                    attempt["error"] = type(exc).__name__
                    attempt_elapsed = asyncio.get_running_loop().time() - attempt_started
                    # A stage timeout consumed most of the allowance; retrying
                    # would only duplicate a doomed request and violate the
                    # router's established deadline behavior.
                    quick_failure = attempt_elapsed < self.decisions_timeout * 0.75
                    if (attempts != 1 or not quick_failure or
                            deadline - asyncio.get_running_loop().time() <= 0):
                        raise
                    retry_transport = True
                    audit["retry"] = {"attempt": 2, "reason": type(exc).__name__}
                except asyncio.CancelledError:
                    attempt["error"] = "deadline-exceeded" if asyncio.get_running_loop().time() >= deadline else "cancelled"
                    raise
                finally:
                    attempt["latencyMs"] = round((asyncio.get_running_loop().time() - attempt_started) * 1000, 1)
                    if retry_session is not None:
                        await retry_session.close()
        except ProviderRequestError as exc:
            if exc.account_blocked:
                self._decisions_failure = exc
            audit["providerError"] = exc.public()
            raise
        except BaseException as exc:
            audit["error"] = "deadline-exceeded" if isinstance(exc, asyncio.CancelledError) and asyncio.get_running_loop().time() >= deadline else type(exc).__name__
            raise
        finally:
            audit["latencyMs"] = round((time.perf_counter() - started) * 1000, 1)


def create_turn_router(api_key, *, backend=None, **kwargs):
    selected = backend or os.getenv("EGOASSIST_TURN_ROUTER_BACKEND", "qwen")
    if selected == "qwen":
        return SemanticTurnRouter(api_key, **kwargs)
    if selected == "openrouter-decisions":
        return DecisionsTurnRouter(api_key, **kwargs)
    raise ValueError("EGOASSIST_TURN_ROUTER_BACKEND must be qwen or openrouter-decisions")
