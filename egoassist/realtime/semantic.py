"""Constrained semantic routing for turn controls and Skill disclosure."""

from __future__ import annotations

import asyncio
import inspect
from contextvars import ContextVar
import json
import os
import time
from typing import Any, Callable, Mapping

from egoassist.core.contracts import IntentCandidate
from egoassist.prompts import prompt_text
from .router_protocol import EVENT_CONTENT_FIELDS, ROUTER_PROMPT, expand_decision


TURN_ROUTER_POLICY_VERSION = "semantic-turn-router-v20"
TASK_CONTROLS = {
    "none", "confirm-step", "rollback", "repeat",
    "pause-task", "resume-task", "request-help", "show-reference",
}


def is_fast_turn_control(text: str) -> bool:
    """Recognize the small set of unambiguous transport controls locally.

    This is intentionally not a general routing vocabulary. It only prevents
    a stop/pause/cancel command from being held behind the normal speech batch;
    JEV still owns the final semantic decision.
    """
    value = "".join(str(text).casefold().split())
    if not value:
        return False
    return any(token in value for token in (
        "停止当前语音", "停止播报", "别说了", "不要说了", "停一下",
        "暂停当前回答", "取消当前回答", "取消回答", "停止回答",
        "stop speaking", "stop talking", "be quiet", "cancel answer",
        "pause answer",
    ))

from egoassist.memory.drafts import memory_draft_candidate


class TurnAccumulator:
    """Debounce voice fragments into one complete utterance before classification.

    The realtime bridge fires ``transcript_received`` once per recognised chunk.
    Without accumulation each chunk is classified and enqueued independently,
    breaking multi-sentence or multi-clause utterances across separate turns.

    A fragment is appended to an internal buffer.  A ``loop.call_later`` timer
    is (re-)armed on every push.  When the timer fires — or the buffer exceeds
    *max_chars* — the joined text is forwarded to *flush_callback*.

    Usage (inside an async WebSocket handler)::

        acc = TurnAccumulator(lambda text: do_enqueue(text))
        # on each speech fragment:
        acc.push(fragment)
        # on connection close (finally block):
        acc.flush_now()

    The env-var ``EGOASSIST_TURN_ACCUMULATE_MS`` overrides the default 1 200 ms
    window at the call site; this class takes the resolved value directly.
    """

    def __init__(
        self,
        flush_callback: Callable[[str], None],
        *,
        window_ms: float = 1200.0,
        max_chars: int = 800,
        priority_predicate: Callable[[str], bool] | None = None,
    ) -> None:
        # Env-var override (also honoured when callers pass the old default 1200).
        try:
            _env = os.getenv("EGOASSIST_TURN_ACCUMULATE_MS")
            if _env:
                window_ms = float(_env)
        except (TypeError, ValueError):
            pass
        if window_ms <= 0:
            raise ValueError("TurnAccumulator window_ms must be positive")
        self._flush_callback = flush_callback
        self.window_ms = window_ms
        self.max_chars = max_chars
        self.priority_predicate = priority_predicate
        self._buffer: list[str] = []
        self._handle: Any = None  # asyncio.TimerHandle | None

    def push(self, fragment: str) -> None:
        """Append *fragment* and reset (or arm) the debounce window."""
        cleaned = fragment.strip()
        if not cleaned:
            return
        self._buffer.append(cleaned)
        # Output controls must not wait for the normal conversational debounce
        # window.  This is deliberately limited to control intent detection;
        # ordinary content still gets the full Gemini-Live-style quiet period.
        if self.priority_predicate is not None and self.priority_predicate(cleaned):
            self._cancel_handle()
            self._do_flush()
            return
        combined_len = sum(len(f) for f in self._buffer) + len(self._buffer) - 1
        if combined_len >= self.max_chars:
            # Buffer is full — flush immediately without waiting for the timer
            self._cancel_handle()
            self._do_flush()
        else:
            # Restart the debounce window
            self._cancel_handle()
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (e.g. unit tests); flush inline
                self._do_flush()
                return
            self._handle = loop.call_later(self.window_ms / 1000.0, self._do_flush)

    def flush_now(self) -> None:
        """Immediately flush any buffered fragments (call on connection teardown)."""
        self._cancel_handle()
        self._do_flush()

    def discard(self) -> None:
        """Discard fragments only during an explicit case reset or shutdown."""
        self._cancel_handle()
        self._buffer.clear()

    def _cancel_handle(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _do_flush(self) -> None:
        self._handle = None
        if not self._buffer:
            return
        merged = " ".join(self._buffer)
        self._buffer.clear()
        try:
            self._flush_callback(merged)
        except Exception:
            pass  # never let a flush exception break the event loop


class SemanticTurnRouter:
    """Classify a turn without maintaining a phrase vocabulary."""

    def reset_failure(self):
        self._blocked_failure = None

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        generation_call: Callable[..., Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = str(api_key)
        self.model = model or os.getenv(
            "EGOASSIST_TURN_ROUTER_MODEL",
            os.getenv("EGOASSIST_SKILL_ROUTER_MODEL", "qwen3-max"),
        )
        self._generation_call = generation_call
        self._http_session = None
        self._blocked_failure = None
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None
            else os.getenv("EGOASSIST_TURN_ROUTER_TIMEOUT", "4.0")
        )
        if self.timeout_seconds <= 0:
            raise ValueError("turn router timeout must be positive")
        self._turn_context: ContextVar[dict[str, Any]] = ContextVar(
            f"semantic-turn-context-{id(self)}", default={}
        )

    def set_turn_context(
        self,
        *,
        clock_context: dict[str, Any] | None,
        memory_draft: dict[str, Any] | None,
        previous_turn: dict[str, Any] | None = None,
        previous_answer: dict[str, Any] | None = None,
        answers: list[dict] | None = None,
        recent_messages: list[dict] | None = None,
        playback: dict | None = None,
        output_permission: dict | None = None,
        scene: dict | None = None,
        memory_candidates: list[dict] | None = None,
        memory_vocabulary: list[dict] | None = None,
        active_task: dict | None = None,
        input_channel: str | None = None,
        recent_interactions: list[dict] | None = None,
    ) -> None:
        self._turn_context.set({
            "clock": dict(clock_context or {}) or None,
            "memoryDraft": dict(memory_draft or {}) or None,
            "previousTurn": dict(previous_turn or {}) or None,
            "previousAnswer": dict(previous_answer or {}) or None,
            "answers": list(answers or []),
            "recentMessages": list(recent_messages or []),
            "playback": playback,
            "outputPermission": output_permission,
            "scene": scene,
            "memoryCandidates": list(memory_candidates or []),
            "memoryVocabulary": list(memory_vocabulary or []),
            "activeTask": active_task,
            "inputChannel": input_channel,
            "recentInteractions": list(recent_interactions or []),
        })

    # ── Main classifier ─────────────────────────────────────────────────────

    async def classify(self, text: str, skill_catalog: list[dict[str, str]]) -> dict[str, Any]:
        started = time.perf_counter()
        request_metrics = {}
        raw = None
        try:
            raw = await asyncio.wait_for(
                self._classify_request(str(text), skill_catalog, request_metrics),
                timeout=self.timeout_seconds,
            )
            wire_decision = json.loads(raw)
            request_metrics['generationMs'] = round((time.perf_counter() - started) * 1000, 1)
            parsed = expand_decision(wire_decision)
            task_reviewed = False
            active_task = self._turn_context.get().get("activeTask")
            if active_task and active_task.get("currentStep") and (
                parsed.get("taskControl") == "confirm-step"
                or (parsed.get("interaction") or {}).get("action") in {"pause", "resume"}
            ):
                review = await self._verify_task_control(str(text), parsed, request_metrics)
                task_reviewed = True
                if parsed.get("taskControl") == "confirm-step" and review.get("taskControl") != "confirm-step":
                    parsed["taskControl"] = "none"
                if review.get("taskDirected") is True and review.get("taskControl") in {"pause-task", "resume-task"}:
                    parsed["taskControl"] = review["taskControl"]
                    parsed["interaction"] = {"audience": "assistant", "action": "reply", "output": "speech"}
            scene = self._turn_context.get().get("scene") or {}
            if scene and (scene.get("speaker") or {}).get("status") != "account-input" and not parsed.get("interaction"):
                raise ValueError("microphone interaction decision is required")
            if not isinstance(parsed, dict):
                raise ValueError("router output must be a JSON object")
            legacy_fields = {
                "speechControl", "taskControl", "preferenceIntent",
                "memoryQuery", "memoryKind", "memoryText", "memoryKey",
                "memoryScope", "memoryConsent", "skillControl", "skill",
            }
            structured_fields = {"memoryDraft", "memoryAtoms", "memoryQuerySpec", "intents", "revision", "interaction"}
            lifecycle_fields = {"intentKind"}
            if not legacy_fields.issubset(parsed) or set(parsed) - (
                legacy_fields | structured_fields | lifecycle_fields
            ):
                raise ValueError("router output has unexpected fields")
            # Optional memory structures cannot invalidate a separately valid control.
            # Reject their contents, retain the raw output and expose component errors.
            component_errors = []
            memory_components = {}
            for field, normalizer, empty in (
                ("memoryAtoms", lambda value: self._normalize_atoms(value, str(text),
                    reference_cards=[*self._turn_context.get().get("memoryCandidates", []),
                        *((scene.get("observationContext") or {}).get("referenceReview") or {}).get("memoryCandidates", [])]), []),
                ("memoryDraft", self._normalize_draft, None),
                ("memoryQuerySpec", self._normalize_query_spec, None),
            ):
                try:
                    memory_components[field] = normalizer(parsed.get(field))
                except (ValueError, TypeError, KeyError) as exc:
                    memory_components[field] = empty
                    component_errors.append({"component": field, "error": str(exc)[:200]})
            memory_atoms = memory_components["memoryAtoms"]
            memory_draft = memory_components["memoryDraft"]
            memory_query_spec = memory_components["memoryQuerySpec"]
            if memory_atoms:
                first = memory_atoms[0]
                parsed.update({
                    "memoryKind": first["kind"],
                    "memoryText": first["text"],
                    "memoryKey": "none",
                    "memoryScope": first["scope"],
                    "memoryConsent": first["consent"],
                })
            elif memory_draft:
                parsed.update({
                    "memoryKind": "capture", "memoryText": "none", "memoryKey": "none",
                    "memoryScope": "participant", "memoryConsent": "explicit",
                })
            speech_control = parsed.get("speechControl")
            task_control = parsed.get("taskControl")
            preference_intent = parsed.get("preferenceIntent")
            memory_query = parsed.get("memoryQuery")
            memory_kind = parsed.get("memoryKind")
            memory_text = parsed.get("memoryText")
            memory_key = parsed.get("memoryKey")
            memory_scope = parsed.get("memoryScope")
            memory_consent = parsed.get("memoryConsent")
            skill_control = parsed.get("skillControl")
            skill = parsed.get("skill")
            intent_kind = parsed.get("intentKind", "foreground-dialogue")
            allowed_skills = {item["name"] for item in skill_catalog}
            if speech_control not in {"stop-speaking", "none"}:
                raise ValueError("invalid speech control label")
            if task_control not in TASK_CONTROLS:
                raise ValueError("invalid task control label")
            if preference_intent not in {"propose-durable", "none"}:
                raise ValueError("invalid preference intent label")
            if memory_query not in {"referential", "none"}:
                raise ValueError("invalid memory query label")
            if memory_kind not in {"none", "capture", "fact", "event", "state", "preference"}:
                raise ValueError("invalid memory kind label")
            if not all(isinstance(value, str) for value in (memory_text, memory_key, memory_scope, memory_consent)):
                raise ValueError("memory card fields must be strings")
            if memory_kind == "none":
                if {memory_text, memory_key, memory_scope, memory_consent} != {"none"}:
                    raise ValueError("empty memory decision must use none sentinels")
            elif memory_kind == "capture":
                if memory_text != "none" or memory_key != "none" or memory_scope != "participant" or memory_consent != "explicit":
                    raise ValueError("invalid explicit memory capture")
            else:
                if not memory_text.strip() or memory_text == "none":
                    raise ValueError("invalid canonical memory text")
                if memory_key != "none" and (not memory_key.strip() or len(memory_key) > 160):
                    raise ValueError("invalid memory key")
                if memory_scope not in {"case", "participant"}:
                    raise ValueError("invalid memory scope")
                if memory_consent not in {"observed", "explicit", "inferred"}:
                    raise ValueError("invalid memory consent")
                if memory_consent == "explicit" and memory_scope != "participant":
                    raise ValueError("explicit memory must use participant scope")
                if memory_consent != "explicit" and memory_scope != "case" and memory_kind != "preference":
                    raise ValueError("observed facts, states, and events must remain case scoped")
            if skill_control not in {"deactivate", "none"}:
                raise ValueError("invalid Skill control label")
            if skill not in {"none", "ambiguous", *allowed_skills}:
                raise ValueError("invalid Skill label")
            if not skill_catalog and skill != "none":
                raise ValueError("Skill selection was not requested")
            if skill_control == "deactivate" and skill != "none":
                raise ValueError("Skill deactivation cannot select a Skill")
            if intent_kind not in {
                "foreground-dialogue", "background-job", "background-reminder", "persistent-task", "waiting-input",
            }:
                raise ValueError("invalid intent lifecycle label")
            intents = self._normalize_intents(parsed.get("intents"), str(text))
            from .interaction import normalize_interaction
            interaction = normalize_interaction(parsed.get("interaction"), self._turn_context.get(), str(text))
            if interaction and interaction["action"] == "cancel":
                valid = await self._verify_cancel(str(text), interaction["targetTurnId"], request_metrics)
                if not valid:
                    interaction = {**interaction, "action": "reply" if parsed.get("intents") else "uncertain"}
                    interaction.pop("targetTurnId", None)
                    if not parsed.get("intents"):
                        interaction["output"] = "silent"
            if interaction:
                parsed["revision"] = None
                speech_control = "none"
                if interaction["action"] in {"listen", "resume", "uncertain", "cancel", "pause", "keep", "wait"}:
                    task_control = "none"
                    skill = "none"
                    if not parsed.get("intents"):
                        memory_atoms = []
                        memory_draft = memory_query_spec = None
            revision = self._normalize_revision(parsed.get("revision"))
            cancellation_only = bool(revision and revision["type"] == "cancellation" and not parsed.get("intents")
                                     and task_control == "none" and not memory_query_spec and not memory_atoms)
            if cancellation_only:
                speech_control = "stop-speaking"
            # A reply-bearing clause must survive a simultaneous audio stop.
            compound_stop = speech_control == "stop-speaking" and bool(
                parsed.get("intents") or task_control != "none" or memory_query_spec
                or memory_draft or memory_atoms or skill != "none"
            )
            if compound_stop:
                speech_control = "none"
            return {
                "method": TURN_ROUTER_POLICY_VERSION,
                "model": self.model,
                "status": "classified",
                "componentErrors": component_errors,
                "protocolNormalizations": (
                    (["memoryQuery-object-to-spec"] if isinstance(wire_decision.get("memoryQuery"), dict) else [])
                    + (["memoryEvents-to-source-events"] if "memoryEvents" in wire_decision else [])
                    + (["ignored-top-level-event-content"] if "memoryEvents" in wire_decision and set(wire_decision) & EVENT_CONTENT_FIELDS else [])
                    + (["compound-stop-preserved-reply"] if compound_stop else [])
                    + (["cancellation-only-silent"] if cancellation_only else [])
                    + (["task-control-reviewed-against-current-step"] if task_reviewed else [])
                ),
                "speechControl": None if speech_control == "none" else speech_control,
                "taskControl": None if task_control == "none" else task_control,
                "preferenceIntent": None if preference_intent == "none" else preference_intent,
                "memoryQuery": None if memory_query == "none" else memory_query,
                "memoryKind": None if memory_kind == "none" else memory_kind,
                "memoryText": None if memory_text == "none" else memory_text.strip(),
                "memoryKey": None if memory_key == "none" else memory_key.strip(),
                "memoryScope": None if memory_scope == "none" else memory_scope,
                "memoryConsent": None if memory_consent == "none" else memory_consent,
                "memoryRequestRejected": any(
                    error["component"] == "memoryAtoms" for error in component_errors
                ) and isinstance(parsed.get("memoryAtoms"), list)
                and any(item.get("consent") == "explicit" for item in parsed["memoryAtoms"] if isinstance(item, dict)),
                "memoryAtoms": memory_atoms,
                "memoryDraft": memory_draft,
                "memoryQuerySpec": memory_query_spec,
                "skillControl": None if skill_control == "none" else skill_control,
                "selectedSkill": skill if skill in allowed_skills else None,
                "intentKind": intent_kind,
                "intents": intents,
                "revision": revision,
                "interaction": interaction,
                "skillStatus": (
                    "matched" if skill in allowed_skills
                    else "ambiguous" if skill == "ambiguous"
                    else "no-match"
                ),
                "rawOutput": raw,
                "requestMetrics": request_metrics,
                "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                "policyVersion": TURN_ROUTER_POLICY_VERSION,
                "claimBoundary": (
                    "Only the current transcript is classified, with explicit clock, pending draft and "
                    "previous-turn context. Labels are hypotheses, not facts."
                ),
            }
        except Exception as exc:
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                request_metrics['deadlineOverrunMs'] = round(max(0, (time.perf_counter() - started - self.timeout_seconds) * 1000), 1)
            return {
                "method": TURN_ROUTER_POLICY_VERSION,
                "model": self.model,
                "status": "unavailable" if self._looks_like_provider_failure(exc) else "invalid",
                "speechControl": None,
                "taskControl": None,
                "preferenceIntent": None,
                "memoryQuery": None,
                "memoryKind": None,
                "memoryText": None,
                "memoryKey": None,
                "memoryScope": None,
                "memoryConsent": None,
                "memoryAtoms": [],
                "memoryDraft": None,
                "memoryQuerySpec": None,
                "skillControl": None,
                "selectedSkill": None,
                "intentKind": "foreground-dialogue",
                "intents": [],
                "revision": None,
                "interaction": {"audience": "uncertain", "action": "uncertain", "output": "silent"},
                "skillStatus": "unavailable",
                "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                "policyVersion": TURN_ROUTER_POLICY_VERSION,
                "claimBoundary": "Classifier failed; retain work and withhold speech until a valid user decision.",
                "error": f"{type(exc).__name__}: {exc}"[:240],
                "rawOutput": raw,
                "timedOut": isinstance(exc, (TimeoutError, asyncio.TimeoutError)),
                "retryable": getattr(exc, "retryable", True),
                "providerError": exc.public() if hasattr(exc, "public") else None,
                "requestMetrics": request_metrics,
            }

    async def aclose(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def _verify_cancel(self, text, target, metrics):
        import aiohttp
        import dashscope
        context = self._turn_context.get()
        candidates = list(context.get("answers", [])) + [context.get("previousAnswer") or {}, context.get("previousTurn") or {}]
        question = next((row.get("text", "") for row in candidates if row.get("turnId") == target), "")
        messages = [{"role": "system", "content": prompt_text("interaction/verifyCancel")},
                    {"role": "user", "content": json.dumps({"question": question, "currentUtterance": text}, ensure_ascii=False)}]
        audit = metrics["cancellationCheck"] = {"messages": messages, "targetTurnId": target, "model": self.model}
        started = time.perf_counter()
        try:
            if not question:
                return False
            async with asyncio.timeout(self.timeout_seconds):
                if self._http_session is None:
                    self._http_session = aiohttp.ClientSession(trust_env=True)
                async with self._http_session.post(dashscope.base_http_api_url.rstrip('/') + '/services/aigc/text-generation/generation',
                    headers={"Authorization": "Bearer " + self.api_key}, json={"model": self.model,
                    "input": {"messages": messages}, "parameters": {"temperature": 0, "enable_thinking": False,
                    "result_format": "message", "max_tokens": 30, "response_format": {"type": "json_object"}}}) as response:
                    response.raise_for_status()
                    payload = await response.json()
                audit["requestId"] = payload.get("request_id")
                audit["usage"] = payload.get("usage")
                audit["rawOutput"] = payload["output"]["choices"][0]["message"]["content"]
                return json.loads(audit["rawOutput"]).get("cancel") is True
        except Exception as exc:
            audit["error"] = type(exc).__name__
            return False
        finally:
            audit["latencyMs"] = round((time.perf_counter() - started) * 1000, 1)

    async def _verify_task_control(self, text, parsed, metrics):
        import aiohttp
        import dashscope
        context = self._turn_context.get()
        messages = [{"role": "system", "content": prompt_text("interaction/verifyTaskControl")},
                    {"role": "user", "content": json.dumps({
                        "activeTask": context["activeTask"], "currentUtterance": text,
                        "recentMessages": context.get("recentMessages", []),
                        "proposedTaskControl": parsed.get("taskControl"),
                        "proposedInteraction": parsed.get("interaction"),
                    }, ensure_ascii=False)}]
        audit = metrics["taskControlCheck"] = {"messages": messages, "model": self.model}
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                parameters = {"temperature": 0, "enable_thinking": False, "result_format": "message",
                              "max_tokens": 80, "response_format": {"type": "json_object"}}
                if self._generation_call is not None:
                    args = dict(model=self.model, messages=messages, **parameters)
                    response = (await self._generation_call(**args) if inspect.iscoroutinefunction(self._generation_call)
                                else await asyncio.to_thread(self._generation_call, **args))
                    if response.status_code != 200:
                        raise ValueError("task control review provider failure")
                    raw = response.output.choices[0].message.content
                else:
                    if self._http_session is None:
                        self._http_session = aiohttp.ClientSession(trust_env=True)
                    async with self._http_session.post(dashscope.base_http_api_url.rstrip('/') + '/services/aigc/text-generation/generation',
                        headers={"Authorization": "Bearer " + self.api_key}, json={"model": self.model,
                        "input": {"messages": messages}, "parameters": parameters}) as response:
                        response.raise_for_status()
                        payload = await response.json()
                    audit["requestId"] = payload.get("request_id")
                    audit["usage"] = payload.get("usage")
                    raw = payload["output"]["choices"][0]["message"]["content"]
                audit["rawOutput"] = raw
                review = json.loads(raw)
                if (not isinstance(review, dict) or review.get("taskControl") not in
                    {"none", "confirm-step", "pause-task", "resume-task"}
                    or not isinstance(review.get("taskDirected"), bool)):
                    raise ValueError("invalid task control review")
                return review
        except Exception as exc:
            audit["error"] = type(exc).__name__
            return {"taskControl": "none", "taskDirected": False}
        finally:
            audit["latencyMs"] = round((time.perf_counter() - started) * 1000, 1)

    async def _classify_request(self, text, skill_catalog, metrics) -> str:
        import aiohttp
        import dashscope
        from egoassist.core.provider_errors import ProviderRequestError

        if self._blocked_failure is not None:
            metrics["circuitOpen"] = True
            metrics["providerError"] = self._blocked_failure.public()
            raise self._blocked_failure

        catalog = [{"name": str(item["name"]), "description": str(item["description"])}
                   for item in skill_catalog]
        context = self._turn_context.get()
        context_data = ({"clock": context.get("clock"),
                         "pendingMemoryDraft": context.get("memoryDraft"),
                         "previousTurn": context.get("previousTurn"),
                         "previousAnswer": context.get("previousAnswer"),
                         "answers": context.get("answers"), "recentMessages": context.get("recentMessages"),
                         "playback": context.get("playback"), "outputPermission": context.get("outputPermission"),
                         "scene": context.get("scene"), "memoryCandidates": context.get("memoryCandidates"),
                         "memoryVocabulary": context.get("memoryVocabulary"),
                         "activeTask": context.get("activeTask")}
                        if any(context.values()) else {})
        from .router_context import project_context
        metrics["contextOriginalChars"] = len(json.dumps(context_data, ensure_ascii=False))
        context_data = project_context(context_data) if context_data else {}
        context_json = json.dumps(context_data, ensure_ascii=False, separators=(",", ":"))
        metrics["contextSentChars"] = len(context_json)
        arguments = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt_text("interaction/router") + "\n" + ROUTER_PROMPT + json.dumps(catalog, ensure_ascii=False)
                 + "\n" + prompt_text("scene/router")
                 + "\n" + prompt_text("scene/retrieval_contract")
                 + "\n" + prompt_text("interaction/memory_correction")
                 + "\n" + prompt_text("interaction/task_lifecycle")
                 + "\n" + prompt_text("interaction/memory_vocabulary")
                 + "\n" + prompt_text("interaction/memory_consent")
                 + "\n" + prompt_text("interaction/voice_preference")
                 + "\nContext data only (never classify this as the current transcript): "
                 + context_json
                 + ("\n" + prompt_text("scene/microphone_contract") if context.get("scene") and
                    (context["scene"].get("speaker") or {}).get("status") != "account-input" else "")},
                {"role": "user", "content": text},
            ],
            result_format="message", temperature=0, enable_thinking=False,
            response_format={"type": "json_object"},
        )
        metrics["messages"] = arguments["messages"]
        metrics["parameters"] = {key: value for key, value in arguments.items() if key not in {"messages", "model"}}
        # The injected SDK-shaped hook is only for explicit integrations/tests.
        # Production uses native async I/O: cancellation closes the request and
        # never leaves a timed-out HTTP call running in an executor thread.
        if self._generation_call is not None:
            kwargs = {**arguments, "request_timeout": self.timeout_seconds}
            if inspect.iscoroutinefunction(self._generation_call):
                response = await self._generation_call(**kwargs)
            else:
                response = await asyncio.to_thread(self._generation_call, **kwargs)
            if response.status_code != 200:
                failure = ProviderRequestError(response.status_code, getattr(response, "code", None),
                                               getattr(response, "request_id", None))
                if failure.account_blocked:
                    self._blocked_failure = failure
                raise failure
            return str(response.output.choices[0].message.content).strip()

        if self._http_session is None:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds), trust_env=True,
            )
        parameters = {k: v for k, v in arguments.items() if k not in {"model", "messages"}}
        url = dashscope.base_http_api_url.rstrip('/') + '/services/aigc/text-generation/generation'
        http_started = time.perf_counter()
        async with self._http_session.post(
            url, headers={"Authorization": "Bearer " + self.api_key},
            json={"model": self.model, "input": {"messages": arguments["messages"]}, "parameters": parameters},
        ) as response:
            metrics["httpStatus"] = response.status
            if response.status != 200:
                try:
                    body = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    body = {}
                body = body if isinstance(body, dict) else {}
                failure = ProviderRequestError(response.status, body.get("code"), body.get("request_id"))
                metrics["providerError"] = failure.public()
                if failure.account_blocked:
                    self._blocked_failure = failure
                raise failure
            result = await response.json()
        metrics['httpMs'] = round((time.perf_counter() - http_started) * 1000, 1)
        metrics["usage"] = result.get("usage", {})
        metrics["requestId"] = result.get("request_id")
        return str(result["output"]["choices"][0]["message"]["content"]).strip()

    @staticmethod
    def _normalize_intents(value: Any, transcript: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("intents must be a list")
        result = []
        for item in value:
            candidate = IntentCandidate.model_validate(item)
            parameters = dict(candidate.parameters)
            if candidate.kind == "background-reminder":
                if set(parameters) != {"delaySeconds", "label"}:
                    raise ValueError("relative reminder parameters must be delaySeconds and label")
                delay = parameters.get("delaySeconds")
                if isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= 31_536_000:
                    raise ValueError("invalid reminder delaySeconds")
                if not isinstance(parameters.get("label"), str) or not parameters["label"].strip():
                    raise ValueError("invalid reminder label")
            result.append(candidate.model_dump())
        return result

    def _normalize_revision(self, value: Any) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"type", "supersedesTurnId"}:
            raise ValueError("invalid revision fields")
        if value.get("type") not in {"correction", "cancellation"}:
            raise ValueError("invalid revision type")
        context = self._turn_context.get()
        allowed = {str((context.get(key) or {}).get("turnId", "")) for key in ("previousTurn", "previousAnswer")} - {""}
        allowed.update(str(item["turnId"]) for item in context.get("answers", []))
        target = str(value.get("supersedesTurnId", ""))
        if target not in allowed:
            raise ValueError("revision must target a supplied previous turn")
        return {"type": value["type"], "supersedesTurnId": target}

    @staticmethod
    def _normalize_atoms(value: Any, source_text: str = "", *, reference_cards=()) -> list[dict[str, Any]]:
        from egoassist.memory.evidence import normalize_context
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("memoryAtoms must be a list")
        result = []
        required = {"kind", "text", "subject", "predicate", "value", "scope", "consent"}
        def replacement(item):
            if item.get("replaces") is None:
                return {}
            if not isinstance(item["replaces"], str):
                raise ValueError("replacement must be a supplied memory card reference")
            target = next((card for card in reference_cards if item["replaces"] in {card.get("id"), card.get("referenceId")}
                and card.get("kind") in {"fact", "state", "preference", "event"}), None)
            if target is None or item["consent"] != "explicit":
                raise ValueError("replacement requires explicit user correction and a supplied memory card id")
            return {"replaces": target["id"]}
        for item in value:
            # Validate consent inside this optional component, before copying its
            # first atom into legacy fields. A bad memory proposal must not mute
            # an otherwise valid dialogue or task-control decision.
            if isinstance(item, dict):
                if item.get("consent") == "explicit" and item.get("scope") != "participant":
                    raise ValueError("explicit memory must use participant scope")
                if (item.get("consent") != "explicit" and item.get("scope") != "case"
                        and item.get("kind") != "preference"):
                    raise ValueError("observed facts, states, and events must remain case scoped")
            if isinstance(item, dict) and item.get("kind") == "event":
                if set(item) - required - {"context", "replaces"} or not {"kind", "scope", "consent"} <= set(item):
                    raise ValueError("invalid event memory fields")
                if item["scope"] not in {"case", "participant"} or item["consent"] not in {"observed", "explicit"}:
                    raise ValueError("invalid event memory scope or consent")
                event_text = source_text if source_text and len(value) == 1 else item.get("text", source_text)
                if not isinstance(event_text, str) or not event_text.strip():
                    raise ValueError("event memory requires source text")
                if source_text and event_text not in source_text:
                    raise ValueError("event excerpt must occur in the source turn")
                if any(item.get(key) is not None and not isinstance(item[key], str) for key in ("subject", "predicate", "value")):
                    raise ValueError("invalid optional event labels")
                result.append({"kind": "event", "text": event_text.strip(),
                               "subject": item.get("subject"), "predicate": item.get("predicate"),
                               "value": item.get("value"), "scope": item["scope"], "consent": item["consent"],
                               "context": normalize_context(item.get("context")), **replacement(item)})
                continue
            if not isinstance(item, dict) or set(item) - {"context", "replaces"} != required:
                raise ValueError("invalid memory atom fields")
            if item["kind"] not in {"fact", "event", "state", "preference"}:
                raise ValueError("invalid memory atom kind")
            if item["scope"] not in {"case", "participant"} or item["consent"] not in {"observed", "explicit", "inferred"}:
                raise ValueError("invalid memory atom scope or consent")
            if not all(isinstance(item[key], str) and item[key].strip() for key in required):
                raise ValueError("memory atom values must be non-empty strings")
            atom = {key: item[key].strip() for key in required}
            atom.update(replacement(item))
            if "context" in item:
                atom["context"] = normalize_context(item["context"])
            result.append(atom)
        return list({json.dumps(atom, sort_keys=True, ensure_ascii=False): atom for atom in result}.values())

    @staticmethod
    def _normalize_draft(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        required = {"mode", "subject", "predicate", "language", "missingFields"}
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("invalid memoryDraft fields")
        if value["mode"] not in {"start", "continue"}:
            raise ValueError("invalid memory draft mode")
        if value["subject"] is not None and not isinstance(value["subject"], str):
            raise ValueError("invalid memory draft subject")
        if not isinstance(value["predicate"], str) or not value["predicate"].strip():
            raise ValueError("memory draft predicate is required")
        missing = value["missingFields"]
        if not isinstance(missing, list) or not missing or not set(missing) <= {"subject", "value"}:
            raise ValueError("invalid memory draft missingFields")
        return {
            "mode": value["mode"],
            "subject": value["subject"].strip() if value["subject"] else None,
            "predicate": value["predicate"].strip(),
            "language": str(value["language"] or "").strip() or None,
            "missingFields": sorted(set(missing)),
        }

    @staticmethod
    def _normalize_query_spec(value: Any) -> dict[str, Any] | None:
        from egoassist.memory.evidence import normalize_context
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {"context"} != {"subject", "predicate"}:
            raise ValueError("invalid memoryQuerySpec fields")
        if any(value[key] is not None and not isinstance(value[key], str) for key in ("subject", "predicate")):
            raise ValueError("invalid memoryQuerySpec values")
        result = {
            "subject": value["subject"].strip() if value["subject"] else None,
            "predicate": value["predicate"].strip() if value["predicate"] else None,
        }
        if "context" in value:
            result["context"] = normalize_context(value["context"])
        return result if any(result.values()) else None

    @staticmethod
    def _looks_like_provider_failure(exc: Exception) -> bool:
        return not isinstance(exc, (json.JSONDecodeError, ValueError, TypeError, KeyError))
