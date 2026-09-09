"""Constrained semantic routing for turn controls and Skill disclosure."""

from __future__ import annotations

import asyncio
import inspect
from contextvars import ContextVar
import json
import os
import re
import time
from typing import Any, Callable, Mapping

from egoassist.memory.timeline import looks_like_question, normalize_identity
from egoassist.product.contracts import IntentCandidate
from egoassist.prompts import prompt_text
from .router_protocol import EVENT_CONTENT_FIELDS, ROUTER_PROMPT, expand_decision


TURN_ROUTER_POLICY_VERSION = "semantic-turn-router-v14"
TASK_CONTROLS = {
    "none", "confirm-step", "rollback", "repeat",
    "pause-task", "resume-task", "request-help", "show-reference",
}

_EXPLICIT_MEMORY_REQUESTS = (
    re.compile(
        r"(?:^|[，。；,;])\s*(?:请(?:你)?|麻烦(?:你)?|你?帮我|给我|"
        r"你(?:都)?(?:要|得|必须|务必|一定要)|我想让你|我要你)\s*"
        r"(?:把|将)?\s*(?:记(?:住|下)?|记录|保存)",
    ),
    re.compile(r"(?:^|[，。；,;])\s*(?:记住|记下|保存)(?!了?吗|了没)"),
    re.compile(r"写进.{0,12}(?:永久)?记忆"),
    re.compile(r"\b(?:please\s+)?(?:remember|save|store|record)\b", re.I),
)
_PHONE_DIGITS = str.maketrans({
    "零": "0", "〇": "0", "一": "1", "幺": "1", "二": "2", "两": "2",
    "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
})
_PHONE_VALUE = re.compile(r"[0-9零〇一幺二两三四五六七八九][0-9零〇一幺二两三四五六七八九\s-]{2,}")

# ── Named constants ───────────────────────────────────────────────────────────
_MAX_MEMORY_ATOMS: int = 3              # max atoms extracted per fallback call


def is_explicit_memory_request(text: str) -> bool:
    """Recognize only direct save/remember commands at the trusted boundary."""

    normalized = " ".join(str(text).split()).strip()
    return bool(normalized and any(pattern.search(normalized) for pattern in _EXPLICIT_MEMORY_REQUESTS))


def memory_draft_candidate(
    text: str,
    draft: Mapping[str, Any] | None,
    pending: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Accept a model draft only when the current turn starts it or makes typed progress."""

    if not draft:
        return None, None
    candidate = dict(draft)
    if candidate.get("mode") == "start":
        if not is_explicit_memory_request(text):
            return None, "draft-start-without-explicit-memory-request"
        return candidate, None
    if not pending:
        return None, "draft-continuation-without-pending-draft"
    expected_predicate = normalize_identity(pending.get("predicate"))
    candidate_predicate = normalize_identity(candidate.get("predicate"))
    expected_subject = normalize_identity(pending.get("subject"))
    candidate_subject = normalize_identity(candidate.get("subject"))
    if not candidate_predicate or candidate_predicate != expected_predicate:
        return None, "draft-continuation-predicate-mismatch"
    if expected_subject and candidate_subject != expected_subject:
        return None, "draft-continuation-subject-mismatch"
    before_missing = set(pending.get("missingFields") or ())
    after_missing = set(candidate.get("missingFields") or ())
    subject_progress = not expected_subject and bool(candidate_subject)
    if not after_missing < before_missing and not subject_progress:
        return None, "draft-continuation-made-no-progress"
    if looks_like_question(text) and not is_explicit_memory_request(text):
        return None, "draft-continuation-is-a-question"
    return candidate, None


def _digits(value: str) -> str:
    return "".join(character for character in value.translate(_PHONE_DIGITS) if character.isdigit())


def _phone_fallback_atoms(text: str, *, phone_context: bool = False) -> list[dict[str, str]]:
    atoms: list[dict[str, str]] = []
    clauses = re.split(r"[，。；,;]|然后", text)
    for index, clause in enumerate(clauses):
        phone_value = _PHONE_VALUE.search(clause)
        if not phone_value:
            continue
        # A numeric value alone does not identify its meaning (locker IDs,
        # prices and dates must never be rewritten as telephone numbers).
        nearby = "，".join(clauses[index:index + 3])
        labelled_phone = bool(re.search(r"电话|手机|小号", clause))
        bare_number = not clause[:phone_value.start()].strip() and not clause[phone_value.end():].strip()
        if not (labelled_phone or (bare_number and re.search(r"电话|手机|小号", nearby))
                or (phone_context and (bare_number or re.search(r"^(?:我的)?号码(?:是|为)", clause)))):
            continue
        number = _digits(phone_value.group(0))
        if len(number) < 3:
            continue
        subject_match = re.search(
            r"([\u4e00-\u9fffA-Za-z0-9]{1,16}?)的(?:电话号码|电话|小号)", clause,
        )
        if "报警电话" in clause:
            subject = "报警电话"
        elif subject_match:
            subject = re.sub(r"^(?:呃|嗯|这是|号码是)", "", subject_match.group(1)).strip()
        else:
            nearby = "，".join(clauses[index:index + 3])
            trailing = re.search(r"这是([\u4e00-\u9fffA-Za-z0-9]{1,16}?)的小号", nearby)
            subject = trailing.group(1) if trailing else "用户显式声明"
        atoms.append({
            "kind": "fact",
            "text": f"{subject}的电话号码是{number}。",
            "subject": subject,
            "predicate": "phone",
            "value": number,
            "scope": "participant",
            "consent": "explicit",
            "fallback": "explicit-memory-fallback-v1",
        })
        if len(atoms) == _MAX_MEMORY_ATOMS:
            break
    return atoms


def _en_explicit_memory_fallback_atoms(
    text: str,
    pending: Mapping[str, Any] | None = None,
    *,
    allow_verbatim: bool = True,
) -> list[dict[str, str]]:
    """English fallback: captures explicit save requests as verbatim facts.

    Structured parsing (phone, location, medication) delegates to the model;
    this fallback only runs when the model call fails entirely.
    """
    normalized = " ".join(str(text).split()).strip()
    if not is_explicit_memory_request(normalized) and not (
        pending and not looks_like_question(normalized)
    ):
        return []
    if not allow_verbatim:
        return []
    return [{
        "kind": "fact",
        "text": normalized,
        "subject": normalize_identity((pending or {}).get("subject")) or "__user_explicit__",
        "predicate": normalize_identity((pending or {}).get("predicate")) or "verbatim",
        "value": normalized,
        "scope": "participant",
        "consent": "explicit",
        "fallback": "explicit-memory-fallback-v1",
    }]


def explicit_memory_fallback_atoms(
    text: str,
    pending: Mapping[str, Any] | None = None,
    *,
    allow_verbatim: bool = True,
    locale: str | None = None,
) -> list[dict[str, str]]:
    """Persist exact user-provided content when the semantic provider fails.

    The active locale is resolved from *locale*, then ``EGOASSIST_FALLBACK_LOCALE``
    env var, defaulting to ``"zh-CN"``.  Non-Chinese deployments should set
    ``EGOASSIST_FALLBACK_LOCALE=en``; the English fallback preserves verbatim
    text only — structured phone/location/medication parsing delegates to the model.
    """
    _locale = (locale or os.getenv("EGOASSIST_FALLBACK_LOCALE", "zh-CN")).strip()
    if _locale not in {"zh-CN", "zh"}:
        return _en_explicit_memory_fallback_atoms(text, pending, allow_verbatim=allow_verbatim)

    normalized = " ".join(str(text).split()).strip()
    explicit = is_explicit_memory_request(normalized)
    predicate = normalize_identity((pending or {}).get("predicate"))
    pending_compatible = bool(
        pending
        and not looks_like_question(normalized)
        and (
            predicate == "memory.value"
            or (predicate in {"phone", "telephone", "contact.phone"} and (
                "电话" in normalized or "号码" in normalized or len(_digits(normalized)) >= 4
            ))
        )
    )
    if not explicit and not pending_compatible:
        return []
    phone_atoms = _phone_fallback_atoms(
        normalized, phone_context=predicate in {"phone", "telephone", "contact.phone"},
    )
    if phone_atoms:
        return phone_atoms

    atoms: list[dict[str, str]] = []
    for clause in re.split(r"[，。；,;]", normalized):
        location = re.search(r"([^\s，。；]{1,24}?)放在([^，。；]{1,80})", clause)
        if location:
            subject = re.sub(r"^(?:我把|把|这个)", "", location.group(1)).strip() or "物品"
            atoms.append({
                "kind": "state", "text": clause.strip() + "。", "subject": subject,
                "predicate": "location", "value": location.group(2).strip(),
                "scope": "participant", "consent": "explicit",
                "fallback": "explicit-memory-fallback-v1",
            })
        schedule = re.search(r"每天([^，。；]{0,16}?)(?:提醒我)?(?:吃|服用)([^，。；]{1,24})", clause)
        if schedule:
            atoms.append({
                "kind": "preference", "text": clause.strip() + "。", "subject": "用户",
                "predicate": "medication.schedule",
                "value": f"每天{schedule.group(1).strip()}服用{schedule.group(2).strip()}",
                "scope": "participant", "consent": "explicit",
                "fallback": "explicit-memory-fallback-v1",
            })
        if "今天" in clause and re.search(r"(?:已经)?(?:吃过|服用过|吃了|服用了)", clause):
            drug = "药"
            named = re.search(r"(?:吃过|服用过|吃了|服用了)([\u4e00-\u9fffA-Za-z0-9]{1,24})", clause)
            if named:
                candidate = re.sub(r"^(?:这个|了)", "", named.group(1)).strip()
                if candidate and candidate not in {"药", "这个药"}:
                    drug = candidate
            atoms.append({
                "kind": "state", "text": clause.strip() + "。", "subject": drug,
                "predicate": "taken", "value": "是",
                "scope": "participant", "consent": "explicit",
                "fallback": "explicit-memory-fallback-v1",
            })
        if len(atoms) >= _MAX_MEMORY_ATOMS:
            return atoms[:_MAX_MEMORY_ATOMS]
    if atoms:
        return atoms[:_MAX_MEMORY_ATOMS]
    if not allow_verbatim:
        return []
    return [{
        "kind": "fact",
        "text": normalized,
        "subject": (normalize_identity((pending or {}).get("subject")) if not explicit else None) or "用户显式声明",
        "predicate": (predicate if not explicit else None) or "verbatim",
        "value": normalized,
        "scope": "participant",
        "consent": "explicit",
        "fallback": "explicit-memory-fallback-v1",
    }]


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
        window_ms: float = 600.0,
        max_chars: int = 800,
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
        self._buffer: list[str] = []
        self._handle: Any = None  # asyncio.TimerHandle | None

    def push(self, fragment: str) -> None:
        """Append *fragment* and reset (or arm) the debounce window."""
        cleaned = fragment.strip()
        if not cleaned:
            return
        self._buffer.append(cleaned)
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
        """Cancel the pending debounce timer and silently drop buffered fragments.

        Used when a stop command is detected early in transcript_received so
        that any preceding partial utterance does not fire as a stale turn.
        """
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
            os.getenv("EGOASSIST_SKILL_ROUTER_MODEL", "qwen-flash"),
        )
        self._generation_call = generation_call
        self._http_session = None
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None
            else os.getenv("EGOASSIST_TURN_ROUTER_TIMEOUT", "3.0")
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
    ) -> None:
        self._turn_context.set({
            "clock": dict(clock_context or {}) or None,
            "memoryDraft": dict(memory_draft or {}) or None,
            "previousTurn": dict(previous_turn or {}) or None,
        })

    # Stop-speaking commands. Used by the input handler (run_egoassist.py) to
    # cancel an in-progress answer the instant a barge-in stop arrives, bypassing
    # the TurnAccumulator debounce. This is input-side cancellation only; it does
    # NOT short-circuit classification — every turn is still routed through the
    # model so the router keeps no local decision vocabulary (see class docstring
    # and test_semantic_control_has_no_local_phrase_override).
    _STOP_RE = re.compile(
        r"^(?:停+|别说了?|闭嘴|住口|安静+|噤声|停一下|停下来?|不用说了?|不用了"
        r"|stop|shut up|be quiet|stop (?:talking|speaking)|quiet(?:ly)?|silence|enough)$",
        re.IGNORECASE,
    )

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
            parsed = expand_decision(wire_decision)
            if not isinstance(parsed, dict):
                raise ValueError("router output must be a JSON object")
            legacy_fields = {
                "speechControl", "taskControl", "preferenceIntent",
                "memoryQuery", "memoryKind", "memoryText", "memoryKey",
                "memoryScope", "memoryConsent", "skillControl", "skill",
            }
            structured_fields = {"memoryDraft", "memoryAtoms", "memoryQuerySpec", "intents", "revision"}
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
                ("memoryAtoms", lambda value: self._normalize_atoms(value, str(text)), []),
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
                if not memory_text.strip() or memory_text == "none" or len(memory_text) > 500:
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
            revision = self._normalize_revision(parsed.get("revision"))
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
                "memoryAtoms": memory_atoms,
                "memoryDraft": memory_draft,
                "memoryQuerySpec": memory_query_spec,
                "skillControl": None if skill_control == "none" else skill_control,
                "selectedSkill": skill if skill in allowed_skills else None,
                "intentKind": intent_kind,
                "intents": intents,
                "revision": revision,
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
                "skillStatus": "unavailable",
                "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                "policyVersion": TURN_ROUTER_POLICY_VERSION,
                "claimBoundary": "Classifier failed open to ordinary conversation; it cannot cause silence.",
                "error": f"{type(exc).__name__}: {exc}"[:240],
                "rawOutput": raw,
                "timedOut": isinstance(exc, (TimeoutError, asyncio.TimeoutError)),
                "requestMetrics": request_metrics,
            }

    async def aclose(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def _classify_request(self, text, skill_catalog, metrics) -> str:
        import aiohttp
        import dashscope

        catalog = [{"name": str(item["name"]), "description": str(item["description"])}
                   for item in skill_catalog]
        context = self._turn_context.get()
        context_data = ({"clock": context.get("clock"),
                         "pendingMemoryDraft": context.get("memoryDraft"),
                         "previousTurn": context.get("previousTurn")}
                        if any(context.values()) else {})
        arguments = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": ROUTER_PROMPT + json.dumps(catalog, ensure_ascii=False)
                 + "\n" + prompt_text("answer_completion/revision")
                 + "\nContext data only (never classify this as the current transcript): "
                 + json.dumps(context_data, ensure_ascii=False)},
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
                raise RuntimeError(str(getattr(response, "message", "semantic turn router failed")))
            return str(response.output.choices[0].message.content).strip()

        if self._http_session is None:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds), trust_env=True,
            )
        parameters = {k: v for k, v in arguments.items() if k not in {"model", "messages"}}
        url = dashscope.base_http_api_url.rstrip('/') + '/services/aigc/text-generation/generation'
        async with self._http_session.post(
            url, headers={"Authorization": "Bearer " + self.api_key},
            json={"model": self.model, "input": {"messages": arguments["messages"]}, "parameters": parameters},
        ) as response:
            metrics["httpStatus"] = response.status
            if response.status != 200:
                # Do not put credentials or arbitrary remote body text in logs.
                raise RuntimeError(f"turn router HTTP {response.status}")
            result = await response.json()
        metrics["usage"] = result.get("usage", {})
        metrics["requestId"] = result.get("request_id")
        return str(result["output"]["choices"][0]["message"]["content"]).strip()

    @staticmethod
    def _normalize_intents(value: Any, transcript: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 4:
            raise ValueError("intents must contain at most four items")
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
        previous_id = str((self._turn_context.get().get("previousTurn") or {}).get("turnId", ""))
        if not previous_id or str(value.get("supersedesTurnId")) != previous_id:
            raise ValueError("revision must target the supplied previous turn")
        return {"type": value["type"], "supersedesTurnId": previous_id}

    @staticmethod
    def _normalize_atoms(value: Any, source_text: str = "") -> list[dict[str, Any]]:
        from egoassist.memory.evidence import normalize_context
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 3:
            raise ValueError("memoryAtoms must contain at most three items")
        result = []
        required = {"kind", "text", "subject", "predicate", "value", "scope", "consent"}
        for item in value:
            if isinstance(item, dict) and item.get("kind") == "event":
                if set(item) - required - {"context"} or not {"kind", "scope", "consent"} <= set(item):
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
                result.append({"kind": "event", "text": event_text.strip()[:500],
                               "subject": item.get("subject"), "predicate": item.get("predicate"),
                               "value": item.get("value"), "scope": item["scope"], "consent": item["consent"],
                               "context": normalize_context(item.get("context"))})
                continue
            if not isinstance(item, dict) or set(item) - {"context"} != required:
                raise ValueError("invalid memory atom fields")
            if item["kind"] not in {"fact", "event", "state", "preference"}:
                raise ValueError("invalid memory atom kind")
            if item["scope"] not in {"case", "participant"} or item["consent"] not in {"observed", "explicit", "inferred"}:
                raise ValueError("invalid memory atom scope or consent")
            if not all(isinstance(item[key], str) and item[key].strip() for key in required):
                raise ValueError("memory atom values must be non-empty strings")
            if len(item["text"]) > 500 or len(item["subject"]) > 160 or len(item["predicate"]) > 80:
                raise ValueError("memory atom is too long")
            atom = {key: item[key].strip() for key in required}
            if "context" in item:
                atom["context"] = normalize_context(item["context"])
            result.append(atom)
        return result

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
