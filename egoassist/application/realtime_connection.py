from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from egoassist.realtime import (
    CaseWorkLedger,
    SemanticTurnRouter,
    SkillRuntime,
    TurnAccumulator,
    is_fast_turn_control,
    executable_tool_declarations,
    livekit_capability,
)
from egoassist.realtime.episodes import RealtimeEpisodeMemory
from egoassist.realtime.decisions_router import DECISIONS_SESSION, create_turn_router
from egoassist.realtime.media_audit import MediaAuditAccumulator
from egoassist.realtime.observation_service import ObservationService
from egoassist.realtime.observation_interaction import ObservationInteraction
from egoassist.realtime.performance import event_metric
from egoassist.realtime.tool_model import ToolModel
from egoassist.realtime.tools import tool_result_succeeded
from egoassist.realtime.turn_coordinator import TurnCoordinator, TurnState


@dataclass
class RealtimeConnection:
    LOGGER: Any
    OmniBrowserBridge: Any
    TaskSlot: Any
    WSMsgType: Any
    ambient_options: Any
    ambient_watch_session: Any
    answer_executor: Any
    append_memory_audit: Any
    apply_memory_hint: Any
    artifacts_for_runtime: Any
    capability_overrides: Any
    case_owner: Any
    clock_for_session: Any
    grounded_user_instructions: Any
    harness_layers: Any
    identity: Any
    live_bridges: Any
    live_harnesses: Any
    live_sockets: Any
    live_sockets_by_session: Any
    live_sockets_by_user: Any
    livekit_settings: Any
    livekit_transport: Any
    memory_recovery: Any
    model: Any
    perception_context_for_runtime: Any
    persist_tool_audit: Any
    process_transcript: Any
    memory_service: Any
    product_database: Any
    queue_reminder: Any
    refresh_harness_context: Any
    registry: Any
    remember_tool_effect: Any
    request_bridge_response: Any
    request_memory_speech: Any
    request_timeline_consolidation: Any
    runtime_checkpoint: Any
    runtime_tool_declarations: Any
    scoped_episodic_memory: Any
    scoped_state_for_user: Any
    state: Any
    stop_case_connection: Any
    submit_tool_confirmation: Any
    timeline: Any
    trace_event: Any
    turn_policy: Any
    user_key: Any
    voice: Any
    web: Any

    async def realtime(self, request):
        server_loop = asyncio.get_running_loop()
        user = request[self.user_key]
        user_id = user["id"]
        requested_session_id = str(request.query.get("session", "")).strip()
        try:
            session_id = self.identity.claim_context_session(
                user_id, requested_session_id or None
            )
        except PermissionError as exc:
            raise self.web.HTTPForbidden(text=str(exc)) from exc
        owner = self.case_owner(user_id, session_id)
        if owner.socket_token is not None or owner.transitioning:
            raise self.web.HTTPConflict(text="context session is already active")
        socket_token = object()
        owner.epoch += 1
        connection_epoch = owner.epoch
        owner.socket_token = socket_token
        self.live_harnesses[session_id] = owner.harness
        ws = self.web.WebSocketResponse(heartbeat=20, max_msg_size=3000000)
        owner.socket = ws
        try:
            await ws.prepare(request)
        except BaseException:
            if owner.socket_token is socket_token:
                owner.socket_token = None
                owner.socket = None
                self.live_harnesses.pop(session_id, None)
            raise
        if owner.socket_token is not socket_token or owner.epoch != connection_epoch:
            await ws.close(code=1000, message=b"case runtime changed during connection")
            if owner.socket_token is socket_token:
                owner.socket_token = None
                owner.socket = None
                self.live_harnesses.pop(session_id, None)
            return ws
        api_key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            await ws.send_json(
                {"type": "error", "message": "QWEN_API_KEY is not configured"}
            )
            await ws.close()
            if owner.socket_token is socket_token:
                owner.socket_token = None
                owner.socket = None
                self.live_harnesses.pop(session_id, None)
            return ws
        livekit_active = False
        unattributed_outputs = set()
        muted_response_ids = set()
        from egoassist.realtime.interaction import InteractionState

        if owner.interaction is None:
            owner.interaction = InteractionState(owner.journal)
        interaction_state = owner.interaction
        interaction_state.reset_playback()
        resumed_response_ids = {}

        async def emit(payload):
            if (
                owner.socket_token is not socket_token
                or owner.epoch != connection_epoch
            ):
                return
            origin = payload.get("origin") or {}
            if (origin.get("observationReviewId") and payload.get("type") in {"audio", "assistant_transcript"}
                    and not observation_speech_allowed(origin)):
                return
            if not payload.get("replay"):
                interaction_state.observe(payload)
            if payload.get("responseId") in resumed_response_ids:
                origin = resumed_response_ids[payload["responseId"]]
                payload = {**payload, "origin": origin}
            if payload.get("type") == "playback_stop":
                interaction_state.pause()
                muted_response_ids.update(payload.get("responseIds") or [])
                resumed_response_ids.clear()
            if payload.get("type") == "audio" and (
                not interaction_state.allows(
                    origin, payload.get("purpose", "conversation")
                )
            ):
                return
            if (
                payload.get("type") == "audio"
                and payload.get("responseId") in muted_response_ids
            ):
                return
            if payload.get("type") == "response_status":
                muted_response_ids.discard(payload.get("responseId"))
            if payload.get("type") in {
                "audio",
                "assistant_transcript",
                "response_status",
            }:
                if (
                    payload.get("type") in {"audio", "assistant_transcript"}
                    and payload.get("purpose") == "conversation"
                    and (not origin.get("turnId"))
                ):
                    response_id = payload.get("responseId")
                    if response_id not in unattributed_outputs:
                        unattributed_outputs.add(response_id)
                        session_trace(
                            "response.output.unattributed",
                            "case-delivery",
                            {"responseId": response_id},
                        )
                        if not ws.closed:
                            await ws.send_json(
                                {
                                    "type": "error",
                                    "message": "Model response has no source question; output was blocked. Please retry.",
                                }
                            )
                    return
                if (
                    origin.get("generation") is not None
                    and origin["generation"] != owner.output_generation
                    and (not origin.get("background"))
                    and (payload.get("responseId") not in resumed_response_ids)
                ):
                    return
                if payload.get("type") == "response_status" and origin.get("turnId"):
                    (
                        owner.work_ledger or CaseWorkLedger(owner.journal)
                    ).set_turn_delivery(
                        origin.get("answerObligation") or origin["turnId"],
                        "generated"
                        if payload["status"] == "completed"
                        else payload["status"],
                        responseId=payload.get("responseId"),
                    )
            if livekit_active and self.livekit_transport is not None:
                if payload.get("type") in {"audio", "interrupted", "playback_stop"}:
                    try:
                        published = await self.livekit_transport.publish(session_id, payload)
                        if published is not False and payload.get("type") == "audio" and origin.get(
                            "observationReviewId"
                        ):
                            owner.lanes.reviews.audio_sent(
                                origin["observationReviewId"],
                                self.memory_service,
                                user_id=user_id,
                                case_id=session_id,
                                response_id=payload.get("responseId"),
                            )
                    except Exception:
                        self.LOGGER.exception(
                            "LiveKit output publish failed for case %s", session_id
                        )
            if not ws.closed:
                if payload.get("type") == "ready":
                    payload = {
                        **payload,
                        "contextSessionId": session_id,
                        "interactionTransport": "livekit"
                        if livekit_active
                        else "websocket-fallback",
                    }
                if livekit_active and payload.get("type") == "audio":
                    return
                await ws.send_json(payload)
                if payload.get("type") == "audio" and origin.get("observationReviewId"):
                    owner.lanes.reviews.audio_sent(
                        origin["observationReviewId"],
                        self.memory_service,
                        user_id=user_id,
                        case_id=session_id,
                        response_id=payload.get("responseId"),
                    )

        bridge = None
        try:
            experiment_case = self.identity.get_context_session(user_id, session_id)
            _ = experiment_case.get("memoryPolicy")
            journal = owner.journal
            harness = owner.harness
            session_tools = owner.tools
            session_runtime = owner.runtime
            turn_state = TurnState(owner.routing, {})
            turn_state.episode_injection = {
                "status": "no-evidence",
                "query": "",
                "episodeIds": [],
                "evidence": [],
            }
            session_media_configuration: dict = {"audio": {}, "video": {}}
            shared_http = request.app.get(DECISIONS_SESSION)
            semantic_router = create_turn_router(api_key, **({"decisions_session": shared_http}
                if shared_http is not None else {}))
            if owner.memory_bus:
                owner.memory_bus.set_ws_emitter(emit, asyncio.get_event_loop())
            initial_context = harness.build(self.harness_layers(session_runtime))
        except Exception as exc:
            if owner.socket_token is socket_token:
                owner.socket_token = None
                owner.socket = None
                self.live_harnesses.pop(session_id, None)
            if not ws.closed:
                try:
                    await ws.send_json({"type": "error", "message": str(exc)})
                except Exception:
                    pass
            await ws.close()
            return ws
        except BaseException:
            if owner.socket_token is socket_token:
                owner.socket_token = None
                owner.socket = None
                self.live_harnesses.pop(session_id, None)
            await ws.close()
            raise

        def current_memory_policy() -> dict:
            return self.identity.get_context_session(user_id, session_id)[
                "memoryPolicy"
            ]

        def current_episodic_memory(
            policy: dict | None = None,
        ) -> RealtimeEpisodeMemory:
            return self.scoped_episodic_memory(
                user_id, session_id, policy or current_memory_policy()
            )

        session_loop = asyncio.get_running_loop()

        def session_trace(
            event_type, source, payload, parent_event_ids=None, **metadata
        ):
            parents = list(parent_event_ids or [])
            trace_runtime = metadata.pop("runtime", None) or session_runtime
            entry = journal.append(
                "runtime_event",
                {
                    "eventType": event_type,
                    "source": source,
                    "payload": dict(payload),
                    "model": metadata.get("model") or payload.get("model") or self.model,
                    "skill": trace_runtime.skill.name if trace_runtime.skill else None,
                    "task": trace_runtime.task.spec.id if trace_runtime.task else None,
                    "responseId": metadata.get("response_id"),
                    "latencyMs": metadata.get("latency_ms"),
                },
                parent_id=parents[-1] if parents else None,
            )
            measurement = event_metric(
                {
                    "type": event_type,
                    "payload": payload,
                    "eventId": entry["id"],
                    "responseId": metadata.get("response_id"),
                    "latencyMs": metadata.get("latency_ms"),
                    "timestampWall": entry["timestamp"],
                }
            )
            if measurement:
                session_loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        emit({"type": "performance_metric", "metric": measurement})
                    )
                )
            if event_type == "proactive.gate.decision":
                policy = current_memory_policy()
                episodic_settings = policy["components"]["timeline"]
                episode = self.memory_service.remember_scene(current_episodic_memory(policy), payload,
                    policy=policy, source_id=entry['id'], user_id=user_id, session_id=session_id,
                    skill=trace_runtime.skill.name if trace_runtime.skill else None,
                    task=trace_runtime.task.spec.id if trace_runtime.task else None)
                if episode:
                    self.append_memory_audit(journal, "episodic.memory.written", "realtime-episodic-memory",
                        {"episodeId": episode['id'], "kind": "scene", "source": "proactive-visual-gate",
                         "storageScope": episodic_settings['scope'], "evidenceEventId": entry['id']},
                        parent_event_id=entry['id'])
                    server_loop.call_soon_threadsafe(self.request_timeline_consolidation, owner)
            return entry

        observation_interaction = ObservationInteraction(
            ambient_options=self.ambient_options, apply_memory_hint=self.apply_memory_hint,
            bridge=lambda: bridge, clock_for_session=self.clock_for_session,
            current_episodic_memory=current_episodic_memory, current_memory_policy=current_memory_policy,
            interaction_state=interaction_state, journal=journal, owner=owner,
            request_timeline_consolidation=self.request_timeline_consolidation,
            runtime_checkpoint=self.runtime_checkpoint, session_id=session_id, session_runtime=session_runtime,
            session_trace=session_trace, memory_service=self.memory_service, user_id=user_id)
        observation_service = ObservationService(
            ambient_watch_session=self.ambient_watch_session,
            bridge=lambda: bridge, journal=journal, model=self.model,
            owner=owner, perception_context_for_runtime=self.perception_context_for_runtime,
            product_database=self.product_database, session_id=session_id, session_runtime=session_runtime,
            session_trace=session_trace, memory_service=self.memory_service, user_id=user_id,
            interaction=observation_interaction)
        session_perception_context = observation_service.session_perception_context
        session_ambient_context = observation_service.session_ambient_context

        async def session_perception_result(captured, raw):
            from egoassist.application.observation_transport import deliver_observation
            return await deliver_observation(observation_service, captured, raw, ws=ws,
                state=lambda: self.scoped_state_for_user(user_id, session_id))

        def runtime_event_sink(active_runtime: SkillRuntime):

            def sink(event_type, source, payload, parent_event_ids=None, **metadata):
                return session_trace(
                    event_type,
                    source,
                    payload,
                    parent_event_ids,
                    runtime=active_runtime,
                    **metadata,
                )

            return sink

        media_audit = MediaAuditAccumulator(session_trace)

        def session_tool_declarations() -> list[dict]:
            skill_tools = (
                executable_tool_declarations(
                    self.runtime_tool_declarations(session_runtime.skill)
                )
                if session_runtime.skill
                else []
            )
            platform_tools: list[dict] = []
            return skill_tools + platform_tools

        def execute_session_tool(
            name: str, arguments: dict, confirmed: bool, origin: dict | None = None
        ) -> dict:
            from egoassist.core.settings import tool_calls_enabled
            if not tool_calls_enabled():
                raise RuntimeError("工具调用已关闭。")
            if (
                owner.socket_token is not socket_token
                or owner.epoch != connection_epoch
                or owner.transitioning
            ):
                raise RuntimeError("tool call belongs to a stale case connection")
            source = dict(origin or {})
            ledger = owner.work_ledger or CaseWorkLedger(harness.journal)
            source_turn_id = str(source.get("turnId", ""))
            source_intent_id = str(source.get("intentId", ""))
            source_job_id = str(source.get("jobId", ""))
            source_generation = source.get("generation")
            if source_generation is not None:
                try:
                    generation_matches = (
                        int(source_generation) == owner.output_generation
                    )
                except (TypeError, ValueError):
                    generation_matches = False
                if not generation_matches:
                    raise RuntimeError(
                        "tool call belongs to a superseded foreground turn"
                    )
            source_turn = ledger.turns().get(source_turn_id) if source_turn_id else None
            source_intent = (
                ledger.intents().get(source_intent_id) if source_intent_id else None
            )
            source_job = ledger.jobs().get(source_job_id) if source_job_id else None
            if (
                source_turn
                and source_turn.get("supersededBy")
                and (
                    source_turn.get("revisionScope") != "answer"
                    or not source_intent
                    or source_intent.get("kind") == "foreground-dialogue"
                )
            ):
                raise RuntimeError("tool call belongs to a corrected user turn")
            if source_intent and source_intent.get("status") in {
                "suspended",
                "cancelled",
                "failed",
            }:
                raise RuntimeError("tool call belongs to an inactive intent")
            if source_job and source_job.get("status") in {
                "suspended",
                "cancelled",
                "failed",
            }:
                raise RuntimeError("tool call belongs to an inactive job")
            audit_count = len(session_tools.audit)
            declared_tools = source.get("tools")
            if not isinstance(declared_tools, list):
                declared_tools = session_tool_declarations()
            declaration, arguments, blocked = session_tools.prepare(
                name, arguments, declared_tools, confirmed
            )
            if blocked is not None:
                self.persist_tool_audit(owner, audit_count)
                return blocked
            if (
                name == "set_timer"
                and declaration
                and (declaration.get("handler") == "builtin.temporal-reminder.v1")
            ):
                result = self.queue_reminder(
                    owner,
                    seconds=arguments.get("seconds"),
                    label=arguments.get("label", "Reminder"),
                    turn_id=source.get("turnId"),
                )
                session_tools.audit.append(
                    {
                        "time": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        "tool": name,
                        "handler": "builtin.temporal-reminder.v1",
                        "arguments": dict(arguments),
                        "result": dict(result),
                    }
                )
                self.persist_tool_audit(owner, audit_count)
                return result
            if (
                name == "show_reference"
                and declaration
                and (declaration.get("handler") == "builtin.artifact-reference.v1")
            ):
                available = self.artifacts_for_runtime(session_runtime, user_id)
                if not available:
                    raise ValueError(
                        "no artifact is authorized for the current checkpoint"
                    )
                result = {"status": "resolved", "artifact": available[0]}
                session_tools.audit.append(
                    {
                        "time": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        "tool": name,
                        "handler": "builtin.artifact-reference.v1",
                        "arguments": dict(arguments),
                        "result": dict(result),
                    }
                )
                self.persist_tool_audit(owner, audit_count)
                return result
            result = session_tools.execute(
                tool=name,
                arguments=arguments,
                declared_tools=declared_tools,
                confirmed=confirmed,
            )
            self.persist_tool_audit(owner, audit_count)
            return result

        async def execute_session_tool_serialized(
            name: str, arguments: dict, confirmed: bool, origin: dict | None = None
        ) -> dict:
            async with owner.mutation_lock:
                return execute_session_tool(name, arguments, confirmed, origin)

        def session_tool_handler(
            name: str, arguments: dict, confirmed: bool, origin: dict | None = None
        ) -> dict:
            try:
                callback_loop = asyncio.get_running_loop()
            except RuntimeError:
                callback_loop = None
            if callback_loop is server_loop:
                if owner.mutation_lock.locked():
                    raise RuntimeError(
                        "case runtime is changing; tool execution was blocked"
                    )
                return execute_session_tool(name, arguments, confirmed, origin)
            future = asyncio.run_coroutine_threadsafe(
                execute_session_tool_serialized(name, arguments, confirmed, origin),
                server_loop,
            )
            return future.result()

        def session_tool_result_committed(
            call_id: str,
            name: str,
            result: Any,
            is_error: bool,
            journal_entry: dict | None,
        ) -> None:
            source_origin = {}
            if journal_entry:
                source_origin = dict(
                    (journal_entry.get("metadata") or {}).get("origin") or {}
                )
            if isinstance(result, dict) and str(
                result.get("status", "")
            ).casefold() in {"queued", "running"}:
                return None
            ledger = owner.work_ledger or CaseWorkLedger(harness.journal)
            intent_id = source_origin.get("intentId")
            result_entry = None
            if intent_id and intent_id in ledger.intents():
                due_at = (
                    (result or {}).get("dueAt") if isinstance(result, dict) else None
                )
                scheduled_job = None
                if due_at and tool_result_succeeded(result, is_error=is_error):
                    scheduled_job = ledger.create_job(
                        intent_id,
                        kind="timer-delivery",
                        payload={
                            "dueAt": due_at,
                            "label": (result or {}).get("label", ""),
                        },
                        status="queued",
                    )
                result_entry = ledger.enqueue_result(
                    intent_id,
                    {
                        "tool": name,
                        "result": result,
                        "isError": bool(is_error),
                        "jobId": (scheduled_job or {}).get("jobId"),
                    },
                    priority=100 if not is_error else 80,
                    delivery="speech-or-display",
                )
                ledger.set_job_status(
                    source_origin.get("jobId"),
                    "failed" if is_error else "completed",
                    detail={"resultId": result_entry["resultId"]},
                ) if source_origin.get("jobId") in ledger.jobs() else None
            card = self.remember_tool_effect(
                owner,
                call_id=call_id,
                name=name,
                result=result,
                is_error=is_error,
                source_id=(journal_entry or {}).get("id"),
            )
            if (
                card
                and card.get("status") == "active"
                and (card.get("tier") == "short")
            ):
                server_loop.call_soon_threadsafe(
                    self.request_timeline_consolidation, owner
                )
            return result_entry

        def session_result_delivery(result_id: str, status: str) -> None:
            ledger = owner.work_ledger or CaseWorkLedger(harness.journal)
            result = ledger.results().get(str(result_id))
            if result is not None:
                ledger.set_result_status(
                    str(result_id), status, detail={"arbiter": "speech"}
                )
                if status == "delivered":
                    intent_id = str(result.get("intentId", ""))
                    active_jobs = [
                        item
                        for item in ledger.jobs().values()
                        if str(item.get("intentId", "")) == intent_id
                        and item.get("status")
                        not in {"completed", "failed", "cancelled"}
                    ]
                    if intent_id in ledger.intents() and (not active_jobs):
                        ledger.set_intent_status(
                            intent_id, "completed", detail={"resultId": str(result_id)}
                        )

        def session_response_lifecycle(origin: dict, status: str) -> None:
            ledger = owner.work_ledger or CaseWorkLedger(harness.journal)
            if origin.get("observationReviewId") and status != "completed":
                session_loop.call_soon_threadsafe(
                    owner.lanes.reviews.interrupted, origin["observationReviewId"]
                )
            if origin.get("turnId"):
                current = (
                    origin.get("responseId") in resumed_response_ids
                    or origin.get("background")
                    or (
                        owner.socket_token is socket_token
                        and owner.epoch == connection_epoch
                        and (
                            origin.get("generation") in {None, owner.output_generation}
                        )
                    )
                )
                ledger.set_turn_delivery(
                    origin.get("answerObligation") or origin["turnId"],
                    "generated" if status == "completed" and current else "interrupted",
                    responseId=origin.get("responseId"),
                )
                if origin.get("answerObligation"):
                    if status == "completed" and current:
                        self.answer_executor.foreground_finished(
                            harness.journal,
                            origin["answerObligation"],
                            origin.get("responseId"),
                            audio_available=origin.get("audioAvailable"),
                        )
                    else:
                        self.answer_executor.interrupt(
                            harness.journal, origin["answerObligation"]
                        )
            intent_id = str(origin.get("intentId", ""))
            job_id = str(origin.get("jobId", ""))
            if intent_id not in ledger.intents():
                return
            if status == "completed":
                waiting_for_input = (
                    job_id in ledger.jobs()
                    and ledger.jobs()[job_id].get("status") == "waiting-input"
                )
                suspended_intent = (
                    intent_id in ledger.intents()
                    and ledger.intents()[intent_id].get("status") == "suspended"
                )
                if (
                    job_id in ledger.jobs()
                    and (not waiting_for_input)
                    and (
                        ledger.jobs()[job_id].get("status")
                        not in {"completed", "failed", "cancelled"}
                    )
                ):
                    ledger.set_job_status(
                        job_id, "completed", detail={"response": "completed"}
                    )
                active_jobs = [
                    item
                    for item in ledger.jobs().values()
                    if str(item.get("intentId", "")) == intent_id
                    and item.get("status") not in {"completed", "failed", "cancelled"}
                ]
                if (
                    not active_jobs
                    and (not waiting_for_input)
                    and (not suspended_intent)
                ):
                    ledger.set_intent_status(
                        intent_id, "completed", detail={"response": "completed"}
                    )
            elif status == "suspended":
                if job_id in ledger.jobs() and ledger.jobs()[job_id].get(
                    "status"
                ) not in {"completed", "failed", "cancelled"}:
                    ledger.set_job_status(
                        job_id,
                        "suspended",
                        detail={
                            "reason": "response-interrupted",
                            "runtimeCheckpoint": origin.get("runtimeCheckpoint"),
                        },
                    )
                ledger.set_intent_status(
                    intent_id,
                    "suspended",
                    detail={
                        "runtimeCheckpoint": origin.get("runtimeCheckpoint"),
                        "reason": "response-interrupted",
                    },
                )

        classification_limit = asyncio.Semaphore(
            max(1, int(os.getenv("EGOASSIST_CLASSIFICATION_CONCURRENCY", "4")))
        )
        _BG_TASK_LIMIT = 20
        _BG_KINDS = frozenset({"background-job", "persistent-task"})

        turns = TurnCoordinator(
            _BG_KINDS=_BG_KINDS,
            _BG_TASK_LIMIT=_BG_TASK_LIMIT,
            bridge=lambda: bridge,
            classification_limit=classification_limit,
            connection_epoch=connection_epoch,
            current_episodic_memory=current_episodic_memory,
            current_memory_policy=current_memory_policy,
            emit=emit,
            execute_session_tool=execute_session_tool,
            harness=harness,
            interaction_state=interaction_state,
            muted_response_ids=muted_response_ids,
            owner=owner,
            resumed_response_ids=resumed_response_ids,
            runtime_event_sink=runtime_event_sink,
            LOGGER=self.LOGGER,
            TaskSlot=self.TaskSlot,
            answer_executor=self.answer_executor,
            capability_overrides=self.capability_overrides,
            clock_for_session=self.clock_for_session,
            grounded_user_instructions=self.grounded_user_instructions,
            memory_recovery=self.memory_recovery,
            process_transcript=self.process_transcript,
            memory_service=self.memory_service,
            queue_reminder=self.queue_reminder,
            refresh_harness_context=self.refresh_harness_context,
            registry=self.registry,
            request_bridge_response=self.request_bridge_response,
            request_timeline_consolidation=self.request_timeline_consolidation,
            runtime_checkpoint=self.runtime_checkpoint,
            scoped_state_for_user=self.scoped_state_for_user,
            state=self.state,
            timeline=self.timeline,
            turn_policy=self.turn_policy,
            semantic_router=semantic_router,
            session_id=session_id,
            session_media_configuration=session_media_configuration,
            session_runtime=session_runtime,
            session_tool_declarations=session_tool_declarations,
            session_tool_handler=session_tool_handler,
            session_tools=session_tools,
            session_trace=session_trace,
            socket_token=socket_token,
            turn_state=turn_state,
            user_id=user_id,
            ws=ws,
        )
        _do_enqueue_transcript = turns._do_enqueue_transcript
        _run_background_intent = turns._run_background_intent
        enqueue_persisted_turn = turns.enqueue_persisted_turn
        turn_worker_loop = turns.turn_worker_loop

        # Audio providers may finish several ASR segments independently. Keep
        # their evidence together while the transcript quiet window coalesces
        # the text into one conversational turn.
        pending_transcript_segments: list[dict] = []
        pending_transcript_events: list[dict] = []

        def merged_transcript_segment() -> dict | None:
            segments = [item for item in pending_transcript_segments if item]
            pending_transcript_segments.clear()
            if not segments:
                return None
            merged = dict(segments[0])
            starts = [item.get("audioStartMs") for item in segments
                      if item.get("audioStartMs") is not None]
            ends = [item.get("audioEndMs") for item in segments
                    if item.get("audioEndMs") is not None]
            if starts:
                merged["audioStartMs"] = min(starts)
            if ends:
                merged["audioEndMs"] = max(ends)
            generations = {item.get("generation") for item in segments
                           if item.get("generation") is not None}
            if len(generations) == 1:
                merged["generation"] = generations.pop()
            merged["segmentCount"] = len(segments)
            return merged

        def flush_transcript_batch(text: str) -> dict:
            segment = merged_transcript_segment()
            captured = _do_enqueue_transcript(text, segment=segment)
            events = list(pending_transcript_events)
            pending_transcript_events.clear()
            if events and bridge is not None:
                bridge._schedule({
                    "type": "user_transcript",
                    "speaker": "unknown",
                    "channel": "input_audio_transcription",
                    "echoRisk": events[-1].get("echoRisk", "unknown"),
                    "text": text,
                    "turnId": captured["turnId"],
                    "timestamp": captured.get("timestamp"),
                    "persisted": True,
                    "segmentCount": len(events),
                })
            return captured

        _turn_accumulator = TurnAccumulator(
            flush_transcript_batch,
            window_ms=float(os.getenv("EGOASSIST_TURN_ACCUMULATE_MS", "1200")),
            priority_predicate=is_fast_turn_control,
        )

        def transcript_received(text):
            normalized = str(text).strip()
            if not normalized:
                return
            _turn_accumulator.push(normalized)

        def transcript_with_evidence(text, segment):
            if str(text).strip():
                pending_transcript_events.append({"echoRisk": "unknown"})
                if segment:
                    segment = {
                        **segment,
                        "generation": f"{connection_epoch}:{segment.get('generation')}",
                    }
                    pending_transcript_segments.append(segment)
                _turn_accumulator.push(str(text).strip())
                # The provider event is already emitted to the UI. The durable
                # turn identity is assigned once the batch is flushed.
                return None

        def proactive_interval_for() -> float:
            """Task checks have their own cadence, independent of ambient sampling."""
            if session_runtime.task is not None and session_runtime.skill is not None:
                return float(
                    session_runtime.skill.manifest.get("proactive_check_seconds", 4)
                )
            return 4.0

        async def handle_media_payload(payload: dict, *, source: str) -> bool:
            kind = payload.get("type")
            if kind == "audio":
                audio_value = str(payload.get("audio", ""))
                owner.scene.append_audio(
                    audio_value,
                    f"{connection_epoch}:{getattr(bridge, '_dialogue_generation', connection_epoch)}",
                    sample_offset=getattr(bridge, "_replayed_audio_samples", 0),
                    metadata=payload.get("capture"),
                )
                video_flushed = bridge.append_audio(audio_value)
                media_audit.record_audio(len(audio_value), video_flushed=video_flushed)
                if video_flushed:
                    bridge.maybe_check_proactively(proactive_interval_for())
                return True
            if kind == "video":
                image_value = str(payload.get("image", ""))
                owner.scene.append_frame(
                    image_value,
                    payload.get("capture"),
                    # Background observation is opt-in. Disabling it must not
                    # silently start a second automatic vision pipeline.
                    describe=False,
                )
                media_audit.record_video(len(image_value))
                bridge.append_video(image_value)
                if hasattr(bridge, "maybe_observe_ambient"):
                    bridge.maybe_observe_ambient(
                        float(self.ambient_options(owner)["intervalSeconds"])
                    )
                if self.ambient_watch_session(owner):
                    owner.scene.events.schedule()
                bridge.maybe_check_proactively(proactive_interval_for())
                return True
            if kind == "media_gap":
                media = str(payload.get("media", "unknown"))
                gap = {
                    "media": media,
                    "reason": str(payload.get("reason", f"{source}-reported-gap")),
                    "droppedChunks": max(0, int(payload.get("droppedChunks") or 0)),
                    "droppedFrames": max(0, int(payload.get("droppedFrames") or 0)),
                    "transport": str(payload.get("transport") or source),
                }
                session_trace("browser.media.gap", f"{source}-media", gap)
                if not ws.closed:
                    await ws.send_json({"type": "media_gap", **gap})
                return True
            return False

        async def consume_livekit_media() -> None:
            if self.livekit_transport is None:
                return
            async for payload in self.livekit_transport.events(session_id):
                if payload.get("type") == "transport_status":
                    if not ws.closed:
                        await ws.send_json(dict(payload))
                    continue
                await handle_media_payload(dict(payload), source="livekit")

        livekit_media_task = None
        try:
            owner.turn_queue = asyncio.Queue(
                maxsize=max(4, int(os.getenv("EGOASSIST_TURN_QUEUE_CAPACITY", "64")))
            )
            owner.turn_worker = asyncio.create_task(
                turn_worker_loop(owner.turn_queue),
                name=f"egoassist-turn-worker:{session_id}:{connection_epoch}",
            )
            ledger = owner.work_ledger or CaseWorkLedger(harness.journal)
            # Completed turns also own sequence numbers. Restoring only pending
            # work reset this counter to one after every clean server restart.
            owner.turn_sequence = max(owner.turn_sequence,
                max((int(item.get('sequence') or 0) for item in ledger.turns().values()), default=0))
            owner.latest_input_sequence = max(owner.latest_input_sequence, owner.turn_sequence)
            for pending_turn in ledger.recoverable_turns():
                enqueue_persisted_turn(pending_turn)
            bridge = self.OmniBrowserBridge(
                api_key,
                self.model,
                self.voice,
                initial_context["system"],
                emit,
                transcript_received,
            )
            from egoassist.core.settings import tool_calls_enabled
            bridge.auto_approve_tools = tool_calls_enabled()
            bridge.tool_model = ToolModel(api_key) if tool_calls_enabled() else None
            bridge.on_transcript_evidence = transcript_with_evidence
            bridge.interaction_managed = True
            bridge.playback_context = interaction_state.playback_context

            def observation_speech_allowed(origin):
                watch = self.ambient_watch_session(owner)
                options = self.ambient_options(owner)
                kind = origin.get("observationKind", "clarification")
                review = owner.lanes.reviews.rows.get(origin.get("observationReviewId"))
                if not review:
                    return False
                if kind == "reminder":
                    goal = owner.lanes.observation_goal()
                    if review.get("reminderGoalSourceIds", []) != ([goal["sourceId"]] if goal else []):
                        return False
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(review["capturedAt"])).total_seconds() * 1000
                from egoassist.core.settings import observation_speech_max_age_ms
                # Once speech begins, let it finish; stale queued reminders must not start.
                if not review.get("sentAt") and age > observation_speech_max_age_ms(review.get('notification')):
                    return False
                return bool(watch and options["remindersEnabled" if kind == "reminder" else "questionsEnabled"]
                    and origin.get("contextId") == watch.perception_context_id
                    and origin.get("generation") == owner.output_generation)

            def session_output_permission(origin=None, purpose="conversation"):
                if (origin or {}).get("observationReviewId") and not observation_speech_allowed(origin):
                    return False
                return interaction_state.allows(origin, purpose)

            bridge.output_permission = session_output_permission
            owner.bridge = bridge
            if hasattr(bridge, "set_context_harness"):
                bridge.set_context_harness(harness)
            if hasattr(bridge, "set_tool_result_handler"):
                bridge.set_tool_result_handler(session_tool_result_committed)
            if hasattr(bridge, "set_result_delivery_handler"):
                bridge.set_result_delivery_handler(session_result_delivery)
            if hasattr(bridge, "set_response_lifecycle_handler"):
                bridge.set_response_lifecycle_handler(session_response_lifecycle)
            if hasattr(bridge, "set_perception_handlers"):
                bridge.set_perception_handlers(
                    session_perception_context, session_perception_result
                )
            if hasattr(bridge, "set_ambient_context_provider"):
                bridge.set_ambient_context_provider(session_ambient_context)
            (
                owner.work_ledger or CaseWorkLedger(harness.journal)
            ).recover_delivery_claims()
            if hasattr(bridge, "set_tools"):
                bridge.set_tools(session_tool_declarations(), session_tool_handler)
            if hasattr(bridge, "set_trace_sink"):
                bridge.set_trace_sink(session_trace)
            self.live_bridges.add(bridge)
            self.live_sockets.add(ws)
            self.live_sockets_by_user.setdefault(user_id, set()).add(ws)
            self.live_sockets_by_session.setdefault(session_id, set()).add(ws)
            if self.livekit_transport is not None:
                try:
                    await self.livekit_transport.connect(session_id)
                    livekit_active = True
                    self.capability_overrides["realtime-media"] = livekit_capability(
                        self.livekit_settings, connected=True
                    )
                except Exception:
                    self.LOGGER.exception(
                        "LiveKit room connection failed; using WebSocket fallback"
                    )
                    self.capability_overrides["realtime-media"] = {
                        "status": "available",
                        "reason": "LiveKit room connection failed; bounded WebSocket media fallback is active.",
                    }
            await bridge.connect()
            if livekit_active:
                livekit_media_task = asyncio.create_task(
                    consume_livekit_media(),
                    name=f"egoassist-livekit-media:{session_id}:{connection_epoch}",
                )
            self.refresh_harness_context(bridge, harness, session_runtime)
            initial_hint, initial_recall = self.apply_memory_hint(
                session_runtime,
                episodic_memory=current_episodic_memory(),
                enabled=current_memory_policy()["components"]["timeline"][
                    "preference_enabled"
                ],
            )
            if initial_recall:
                journal.append_state_checkpoint(
                    self.runtime_checkpoint(session_runtime), reason="memory-recall"
                )
                self.request_memory_speech(
                    initial_hint, target_bridge=bridge, target_runtime=session_runtime
                )
            await ws.send_json(
                {
                    "type": "state",
                    "state": self.state(
                        session_runtime,
                        routing_override=turn_state.routing,
                        bridge_override=bridge,
                        harness_override=harness,
                        tools_override=session_tools,
                        episodic_override=current_episodic_memory(),
                        media_override=session_media_configuration,
                        user_scope=user_id,
                        memory_policy_override=current_memory_policy(),
                    ),
                }
            )
            async for message in ws:
                if message.type != self.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                kind = payload.get("type")
                if await handle_media_payload(payload, source="websocket"):
                    continue
                if kind == "media_config":
                    raw_video_configuration = payload.get("video", {})
                    video_configuration = (
                        raw_video_configuration
                        if isinstance(raw_video_configuration, dict)
                        else {}
                    )
                    raw_audio_configuration = payload.get("audio", {})
                    audio_configuration = (
                        raw_audio_configuration
                        if isinstance(raw_audio_configuration, dict)
                        else {}
                    )
                    raw_clock = payload.get("clock", {})
                    browser_clock = raw_clock if isinstance(raw_clock, dict) else {}
                    clock_snapshot = self.identity.update_clock_context(
                        user_id,
                        session_id,
                        timezone_name=str(browser_clock.get("timezone") or "UTC"),
                        utc_offset_minutes=int(
                            browser_clock.get("utcOffsetMinutes") or 0
                        ),
                    )
                    mirrored = bool(video_configuration.get("mirrored", False))
                    session_media_configuration.clear()
                    session_media_configuration.update(
                        {
                            "audio": dict(audio_configuration),
                            "video": {
                                "mirrored": mirrored,
                                "previewMatchesModelInput": bool(
                                    video_configuration.get(
                                        "previewMatchesModelInput", False
                                    )
                                ),
                                "coordinateConvention": "horizontally-mirrored"
                                if mirrored
                                else "camera-native",
                            },
                            "clock": clock_snapshot,
                        }
                    )
                    self.trace_event(
                        "media.audio.configuration",
                        "browser-media",
                        {
                            **session_media_configuration,
                            "playbackCapture": bool(
                                payload.get("playbackCapture", False)
                            ),
                        },
                    )
                    session_trace(
                        "clock.context.updated", "browser-clock", clock_snapshot
                    )
                elif kind == "cancel":
                    bridge.cancel_response()
                elif kind == "playback_progress":
                    if interaction_state.progress(payload):
                        row = interaction_state.responses[payload["responseId"]]
                        if row["playedSamples"] > 0 and row["origin"].get(
                            "observationReviewId"
                        ):
                            owner.lanes.reviews.played(
                                row["origin"]["observationReviewId"]
                            )
                        if (
                            row.get("terminalStatus") == "completed"
                            and row["playedSamples"] == len(row["pcm"]) // 2
                            and (not row["overflow"])
                        ):
                            turn_id = row["origin"].get("answerObligation") or row[
                                "origin"
                            ].get("turnId")
                            if turn_id:
                                CaseWorkLedger(harness.journal).set_turn_delivery(
                                    turn_id,
                                    "completed",
                                    channel="device-playback",
                                    responseId=payload["responseId"],
                                    playedSamples=row["playedSamples"],
                                )
                elif kind == "vad_misfire":
                    if getattr(bridge, "interaction_managed", False):
                        continue  # VAD never stopped playback in semantic mode.
                    generation = owner.output_generation

                    async def recover_misfire(expected_generation=generation):
                        await asyncio.sleep(1.0)
                        if (
                            owner.socket_token is not socket_token
                            or owner.output_generation != expected_generation
                            or ws.closed
                        ):
                            return
                        if (
                            not getattr(bridge, "connected", False)
                            or bridge.has_unresolved_user_input()
                            or interaction_state.snapshot()["quiet"]
                        ):
                            return
                        progress = interaction_state.playback_context() or {}
                        tail = interaction_state.tail(progress.get("turnId"))
                        if not tail or not tail[2]:
                            return
                        response_id, row, pcm = tail
                        interaction_state.release()
                        muted_response_ids.discard(response_id)
                        await ws.send_json(
                            {
                                "type": "playback_resume",
                                "turnId": row["origin"].get("turnId"),
                                "responseId": response_id,
                                "offsetSamples": row["playedSamples"],
                            }
                        )
                        import base64

                        for offset in range(0, len(pcm), 24000):
                            await emit(
                                {
                                    "type": "audio",
                                    "responseId": response_id,
                                    "origin": row["origin"],
                                    "purpose": "conversation",
                                    "replay": True,
                                    "audio": base64.b64encode(
                                        pcm[offset : offset + 24000]
                                    ).decode(),
                                }
                            )
                        harness.journal.append(
                            "interaction.resume",
                            {"responseId": response_id, "method": "local-vad-misfire"},
                        )

                    task = asyncio.create_task(recover_misfire())
                    owner.classification_tasks.add(task)
                    task.add_done_callback(owner.classification_tasks.discard)
                elif kind == "speech_activity":
                    if hasattr(bridge, "handle_user_speech_started"):
                        bridge.handle_user_speech_started(trigger="device-vad")
                elif kind == "control":
                    transcript = str(payload.get("transcript", "")).strip()
                    if not transcript:
                        continue
                    ledger = owner.work_ledger or CaseWorkLedger(harness.journal)
                    client_id = str(payload.get("clientRequestId") or "")
                    if client_id and (
                        len(client_id) > 80
                        or not all(
                            (
                                c.isascii() and (c.isalnum() or c in "-_")
                                for c in client_id
                            )
                        )
                    ):
                        await ws.send_json(
                            {"type": "error", "message": "invalid clientRequestId"}
                        )
                        continue
                    previous = ledger.turns().get(client_id) if client_id else None
                    if previous:
                        if previous["text"] != transcript:
                            await ws.send_json(
                                {
                                    "type": "error",
                                    "message": "clientRequestId content mismatch",
                                }
                            )
                        else:
                            await ws.send_json(
                                {
                                    "type": "turn_receipt",
                                    "turnId": client_id,
                                    "deduplicated": True,
                                    "status": previous["status"],
                                }
                            )
                        continue
                    queue = owner.turn_queue
                    if (
                        queue is None
                        or owner.turn_worker is None
                        or owner.turn_worker.done()
                    ):
                        raise RuntimeError("case turn worker is not available")
                    owner.turn_sequence += 1
                    owner.latest_input_sequence = owner.turn_sequence
                    captured = ledger.capture_turn(
                        transcript,
                        source="explicit-control",
                        sequence=owner.turn_sequence,
                        turn_id=client_id or None,
                    )
                    owner.scene.capture(captured["turnId"], explicit=True, metadata=payload.get('capture'))
                    ledger.set_turn_delivery(captured["turnId"], "received")
                    await ws.send_json(
                        {
                            "type": "user_transcript",
                            "speaker": "user",
                            "channel": "explicit_control",
                            "echoRisk": "low",
                            "text": transcript,
                            "turnId": captured["turnId"],
                        }
                    )
                    if client_id:
                        await ws.send_json(
                            {
                                "type": "turn_receipt",
                                "turnId": captured["turnId"],
                                "status": "received",
                            }
                        )
                    if hasattr(bridge, "handle_user_speech_started"):
                        bridge.handle_user_speech_started(trigger="explicit-control")
                    enqueue_persisted_turn(captured, explicit=True)
                elif kind == "tool_confirmation":
                    call_id = str(payload.get("callId", ""))
                    self.submit_tool_confirmation(
                        owner, call_id, payload.get("accepted")
                    )
                    await ws.send_json(
                        {"type": "tool_confirmation_receipt", "callId": call_id}
                    )
                elif kind in {"state", "refresh"}:
                    await ws.send_json(
                        {
                            "type": "state",
                            "state": self.state(
                                session_runtime,
                                routing_override=turn_state.routing,
                                bridge_override=bridge,
                                harness_override=harness,
                                tools_override=session_tools,
                                episodic_override=current_episodic_memory(),
                                media_override=session_media_configuration,
                                user_scope=user_id,
                                memory_policy_override=current_memory_policy(),
                            ),
                        }
                    )
        except Exception as exc:
            if not ws.closed:
                try:
                    await ws.send_json({"type": "error", "message": str(exc)})
                except Exception:
                    pass
        finally:
            _turn_accumulator.flush_now()
            owns_reservation = owner.socket_token is socket_token
            try:
                if livekit_media_task is not None:
                    livekit_media_task.cancel()
                    await asyncio.gather(livekit_media_task, return_exceptions=True)
                if livekit_active and self.livekit_transport is not None:
                    await self.livekit_transport.close(session_id)
                    self.capability_overrides["realtime-media"] = livekit_capability(
                        self.livekit_settings
                    )
                if owns_reservation:
                    owner.transitioning = True
                    await self.stop_case_connection(owner, notify=False)
                try:
                    media_audit.flush()
                except Exception:
                    self.LOGGER.exception(
                        "failed to flush aggregated media audit for case %s", session_id
                    )
                if bridge:
                    self.live_bridges.discard(bridge)
                    await asyncio.gather(
                        *tuple(owner.tool_confirmation_tasks.values()),
                        return_exceptions=True,
                    )
                    await bridge.close()
            finally:
                await semantic_router.aclose()
                self.live_sockets.discard(ws)
                self.live_sockets_by_user.get(user_id, set()).discard(ws)
                self.live_sockets_by_session.get(session_id, set()).discard(ws)
                if not self.live_sockets_by_session.get(session_id):
                    self.live_sockets_by_session.pop(session_id, None)
                if owns_reservation:
                    self.live_harnesses.pop(session_id, None)
                    owner.transitioning = False
                    interaction_state.reset_playback()
        return ws
