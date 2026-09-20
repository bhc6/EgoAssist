from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from egoassist.product.answer_jobs import answer_jobs, answer_job_summaries
from egoassist.prompts import prompt_text
from egoassist.realtime import (
    CaseWorkLedger,
    SkillRuntime,
    normalize_intent_candidates,
    platform_capability_available,
)
from egoassist.realtime.routed_tools import execute_reference_request
from egoassist.realtime.router_context import project_task


@dataclass
class TurnState:
    routing: Any
    episode_injection: Any


@dataclass
class TurnCoordinator:
    _BG_KINDS: Any
    _BG_TASK_LIMIT: Any
    bridge: Any
    classification_limit: Any
    connection_epoch: Any
    current_episodic_memory: Any
    current_memory_policy: Any
    emit: Any
    execute_session_tool: Any
    harness: Any
    interaction_state: Any
    muted_response_ids: Any
    owner: Any
    resumed_response_ids: Any
    runtime_event_sink: Any
    LOGGER: Any
    TaskSlot: Any
    answer_executor: Any
    capability_overrides: Any
    clock_for_session: Any
    grounded_user_instructions: Any
    memory_recovery: Any
    process_transcript: Any
    queue_reminder: Any
    refresh_harness_context: Any
    registry: Any
    request_bridge_response: Any
    request_timeline_consolidation: Any
    runtime_checkpoint: Any
    scoped_state_for_user: Any
    state: Any
    timeline: Any
    turn_policy: Any
    semantic_router: Any
    session_id: Any
    session_media_configuration: Any
    session_runtime: Any
    session_tool_declarations: Any
    session_tool_handler: Any
    session_tools: Any
    session_trace: Any
    socket_token: Any
    turn_state: Any
    user_id: Any
    ws: Any

    def mark_interrupted_turns(self, ledger):
        for turn in ledger.snapshot()["turnInbox"]:
            delivery = turn.get("delivery") or {}
            if delivery.get("status") in {"received", "responding"}:
                ledger.set_turn_delivery(
                    turn["turnId"], "interrupted", reason="newer-user-turn"
                )
                self.answer_executor.interrupt(self.harness.journal, turn["turnId"])

    def _do_enqueue_transcript(self, text: str, segment=None) -> None:
        """Enqueue one complete (already-accumulated) utterance."""
        self.owner.turn_sequence += 1
        self.owner.latest_input_sequence = self.owner.turn_sequence
        self.owner.output_generation += 1
        self.interaction_state.pause()
        ledger = self.owner.work_ledger or CaseWorkLedger(self.harness.journal)
        self.mark_interrupted_turns(ledger)
        ledger.suspend_before_sequence(
            self.owner.turn_sequence, reason="newer-user-turn"
        )
        captured = ledger.capture_turn(
            text, source="input-audio-transcript", sequence=self.owner.turn_sequence
        )
        if segment is not None:
            self.owner.scene.capture(captured["turnId"], segment=segment)
        ledger.set_turn_delivery(captured["turnId"], "received")
        self.enqueue_persisted_turn(captured)

    def refill_turn_queue(self) -> None:
        queue = self.owner.turn_queue
        if queue is None or queue.full():
            return
        ledger = self.owner.work_ledger or CaseWorkLedger(self.harness.journal)
        for pending in ledger.recoverable_turns():
            if queue.full():
                break
            self.enqueue_persisted_turn(pending)

    def enqueue_persisted_turn(
        self, turn: dict, *, explicit: bool = False, future=None
    ) -> bool:
        queue = self.owner.turn_queue
        worker = self.owner.turn_worker
        if (
            queue is not None
            and worker is not None
            and (not worker.done())
            and (self.owner.socket_token is self.socket_token)
            and (self.owner.epoch == self.connection_epoch)
        ):
            turn_id = str(turn["turnId"])
            if turn_id in self.owner.queued_turn_ids:
                return True
            if queue.full():
                (
                    self.owner.work_ledger or CaseWorkLedger(self.harness.journal)
                ).set_turn_status(
                    turn_id, "deferred", detail={"reason": "bounded-turn-queue-full"}
                )
                if future and (not future.done()):
                    future.set_exception(
                        RuntimeError("turn persisted for deferred processing")
                    )
                return False
            sequence = int(turn.get("sequence") or 0)
            self.owner.turn_sequence = max(self.owner.turn_sequence, sequence)
            self.owner.latest_input_sequence = max(
                self.owner.latest_input_sequence, sequence
            )
            semantic_task = asyncio.create_task(
                self.classify_persisted_turn(turn_id, str(turn["text"])),
                name=f"egoassist-turn-classifier:{turn_id}",
            )
            self.owner.classification_tasks.add(semantic_task)
            semantic_task.add_done_callback(self.owner.classification_tasks.discard)
            self.owner.queued_turn_ids.add(turn_id)
            queue.put_nowait(
                {
                    "text": str(turn["text"]),
                    "explicit": bool(explicit),
                    "future": future,
                    "turnSequence": sequence,
                    "turnId": turn_id,
                    "semanticTask": semantic_task,
                }
            )
            return True
        return False

    async def turn_worker_loop(self, queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            future = item.get("future")
            try:
                result = await self.process_queued_turn(
                    item["text"],
                    explicit=item["explicit"],
                    turn_sequence=item["turnSequence"],
                    turn_id=item["turnId"],
                    semantic_task=item.get("semanticTask"),
                )
                if future and (not future.done()):
                    future.set_result(result)
            except asyncio.CancelledError:
                if future and (not future.done()):
                    future.cancel()
                raise
            except Exception as exc:
                try:
                    ledger = self.owner.work_ledger or CaseWorkLedger(
                        self.harness.journal
                    )
                    ledger.set_turn_delivery(item["turnId"], "failed", error=str(exc))
                    self.answer_executor.interrupt(self.harness.journal, item["turnId"])
                    if ledger.turns()[item["turnId"]]["status"] != "completed":
                        ledger.set_turn_status(
                            item["turnId"], "failed", detail={"error": str(exc)}
                        )
                except Exception:
                    self.LOGGER.exception("failed to persist turn failure")
                if future and (not future.done()):
                    future.set_exception(exc)
                elif not self.ws.closed:
                    try:
                        await self.ws.send_json(
                            {
                                "type": "error",
                                "message": str(exc),
                                "turnId": item["turnId"],
                            }
                        )
                    except Exception:
                        pass
            finally:
                self.owner.queued_turn_ids.discard(str(item["turnId"]))
                queue.task_done()
                self.refill_turn_queue()

    async def process_queued_turn(
        self,
        text: str,
        *,
        explicit: bool,
        turn_sequence: int,
        turn_id: str,
        semantic_task: asyncio.Task | None = None,
    ) -> dict:
        text = str(text).strip()
        if not text:
            self.session_trace(
                "audio.transcript.ignored",
                "realtime-user",
                {"reason": "empty-transcript"},
            )
            return {}
        work_ledger = self.owner.work_ledger or CaseWorkLedger(self.harness.journal)
        scene_task = None
        try:
            semantic = (
                await semantic_task
                if semantic_task is not None
                else await self.classify_persisted_turn(turn_id, text)
            )
            applied = self.owner.applied_controls.pop(turn_id, None)
            if applied is not None:
                return {"control": applied, "semanticDecision": semantic}
            control_only = (semantic.get("interaction") or {}).get("action") in {
                "listen",
                "pause",
                "cancel",
                "uncertain",
            } or semantic.get("speechControl")
            scene_task = self.owner.prepared_scenes.pop(turn_id, None)
            if scene_task is None:
                scene_task = asyncio.create_task(
                    self.owner.scene.context(
                        turn_id,
                        describe_frame=not control_only,
                        question=text,
                    )
                )
            if not control_only and (not scene_task.done()):
                await self.turn_progress(turn_id, "waiting-vision")
            shared_scene = (
                await self.owner.scene.context(turn_id)
                if control_only
                else await scene_task
            )
        finally:
            if scene_task is not None:
                if not scene_task.done():
                    scene_task.cancel()
                await asyncio.gather(scene_task, return_exceptions=True)
        if shared_scene:
            shared_scene["speaker"] = self.owner.scene.turns[turn_id]["speaker"]
            if (explicit and not control_only and self.owner.scene.api_key
                    and not shared_scene.get('visual', {}).get('frame')
                    and self.current_memory_policy()['components']['timeline']['episodic_enabled']):
                from egoassist.realtime.router_context import retrieval_text
                await self.turn_progress(turn_id, 'waiting-vision')
                shared_scene['historicalVisual'] = await self.owner.scene.visual_history.observe(
                    turn_id, retrieval_text(text, semantic))
        if (
            self.owner.socket_token is not self.socket_token
            or self.owner.epoch != self.connection_epoch
        ):
            raise asyncio.CancelledError
        _bg_task_queue: list[tuple[self.TaskSlot, str, str, SkillRuntime, dict]] = []
        await self.turn_progress(turn_id, "committing")
        async with self.owner.mutation_lock:
            if (
                self.owner.socket_token is not self.socket_token
                or self.owner.epoch != self.connection_epoch
            ):
                raise asyncio.CancelledError
            turn_generation = self.owner.output_generation
            work_ledger.set_turn_status(turn_id, "processing")
            from egoassist.realtime.interaction_controller import apply_interaction

            handled, control = await apply_interaction(
                semantic,
                turn_id,
                turn_sequence,
                owner=self.owner,
                interaction_state=self.interaction_state,
                harness=self.harness,
                answer_executor=self.answer_executor,
                user_id=self.user_id,
                bridge=self.bridge(),
                ws=self.ws,
                emit=self.emit,
                muted_response_ids=self.muted_response_ids,
                resumed_response_ids=self.resumed_response_ids,
                request_bridge_response=self.request_bridge_response,
            )
            if handled and (not semantic.get("revision")):
                self.turn_state.routing = {
                    **self.turn_state.routing,
                    "method": semantic.get("method"),
                    "status": "suppressed-control",
                    "selectedSkill": None,
                    "selectedTask": None,
                    "interaction": control,
                    "suppressedBy": control["action"],
                }
                self.owner.routing = self.turn_state.routing
                self.resolve_semantic_turn(text, {}, semantic, turn_id)
                work_ledger.set_turn_status(turn_id, "completed")
                work_ledger.set_turn_delivery(
                    turn_id, "completed", channel="speech-control"
                )
                if turn_sequence == self.owner.latest_input_sequence and (
                    not self.ws.closed
                ):
                    await self.ws.send_json(
                        {
                            "type": "state",
                            "state": self.scoped_state_for_user(
                                self.user_id, self.session_id
                            ),
                        }
                    )
                return {"control": "stop-speaking", "semanticDecision": semantic}
            revision = semantic.get("revision")
            if revision:
                work_ledger.revise_turn(
                    turn_id,
                    str(revision["supersedesTurnId"]),
                    revision_type=str(revision.get("type", "correction")),
                    scope="answer",
                )
                previous_id = str(revision["supersedesTurnId"])
                if previous_id in answer_jobs(self.harness.journal):
                    self.answer_executor.cancel(
                        self.harness.journal,
                        previous_id,
                        reason="explicit-" + str(revision["type"]),
                    )
            if semantic.get("speechControl") == "stop-speaking":
                self.bridge().cancel_response(
                    drop_queued_below=101, reason="semantic-stop-speaking"
                )
                self.turn_state.routing = {
                    **self.turn_state.routing,
                    "method": semantic.get("method"),
                    "status": "suppressed-control",
                    "selectedSkill": None,
                    "selectedTask": None,
                    "suppressedBy": "stop-speaking",
                }
                self.owner.routing = self.turn_state.routing
                self.resolve_semantic_turn(text, {}, semantic, turn_id)
                self.session_trace(
                    "user.control.stop_speaking",
                    "semantic-turn-router",
                    {"text": text, "turnId": turn_id, "responseRequested": False},
                )
                work_ledger.set_turn_status(turn_id, "completed")
                work_ledger.set_turn_delivery(
                    turn_id, "completed", channel="speech-control"
                )
                if turn_sequence == self.owner.latest_input_sequence and (
                    not self.ws.closed
                ):
                    await self.ws.send_json(
                        {
                            "type": "speech_control",
                            "turnId": turn_id,
                            "action": "cancel-answer" if revision else "stop-speaking",
                        }
                    )
                    await self.ws.send_json(
                        {
                            "type": "state",
                            "state": self.scoped_state_for_user(
                                self.user_id, self.session_id
                            ),
                        }
                    )
                return {"control": "stop-speaking", "semanticDecision": semantic}
            pending_draft_before = self.timeline.pending_draft(
                user_id=self.user_id, case_id=self.session_id
            )
            intent_candidates = normalize_intent_candidates(text, semantic)
            scheduled_intents = []
            remaining_candidates = []
            for candidate in intent_candidates:
                parameters = dict(candidate.parameters)
                if (
                    candidate.kind == "background-reminder"
                    and parameters.get("delaySeconds") is not None
                    and platform_capability_available(
                        "timer-delivery", self.capability_overrides
                    )
                ):
                    scheduled_intents.append(
                        self.queue_reminder(
                            self.owner,
                            seconds=int(parameters["delaySeconds"]),
                            label=str(parameters.get("label") or "Reminder"),
                            turn_id=turn_id,
                        )
                    )
                else:
                    remaining_candidates.append(candidate)
            semantic = {
                **semantic,
                "intents": [item.model_dump() for item in intent_candidates],
                "acceptedIntents": scheduled_intents,
            }
            if semantic.get("taskControl") == "rollback":
                for _slot in list(self.owner.background_tasks.values()):
                    if _slot.status == "running" and _slot.asyncio_task is not None:
                        _slot.asyncio_task.cancel()
            selected_candidate = next(
                (
                    item
                    for item in remaining_candidates
                    if item.kind in {"foreground-dialogue", "waiting-input"}
                ),
                remaining_candidates[0] if remaining_candidates else None,
            )
            parallel_candidates = [
                c
                for c in remaining_candidates
                if c is not selected_candidate
                and c.kind in self._BG_KINDS
                and platform_capability_available(
                    "background-execution", self.capability_overrides
                )
            ]
            secondary_response_candidates = [
                c
                for c in remaining_candidates
                if c is not selected_candidate
                and (
                    c.kind in {"foreground-dialogue", "waiting-input"}
                    or (
                        c.kind in self._BG_KINDS
                        and (
                            not platform_capability_available(
                                "background-execution", self.capability_overrides
                            )
                        )
                    )
                )
            ]
            requested_intent_kind = str(
                selected_candidate.kind
                if selected_candidate is not None
                else "foreground-dialogue"
            )
            intent_kind = requested_intent_kind
            requested_capability = (
                "timer-delivery"
                if requested_intent_kind == "background-reminder"
                else "background-execution"
            )
            has_required_parameters = not (
                requested_intent_kind == "background-reminder"
                and selected_candidate is not None
                and (selected_candidate.parameters.get("delaySeconds") is None)
            )
            if requested_intent_kind == "background-reminder" and (
                not has_required_parameters
            ):
                intent_kind = "waiting-input"
                semantic = {
                    **semantic,
                    "requestedIntentKind": requested_intent_kind,
                    "intentKind": intent_kind,
                    "intentExecution": {
                        "status": "waiting-input",
                        "capability": "timer-delivery",
                        "reason": "A reminder duration is required before durable execution can start.",
                    },
                }
            elif requested_intent_kind in {
                "background-job",
                "background-reminder",
                "persistent-task",
            } and (
                not platform_capability_available(
                    requested_capability, self.capability_overrides
                )
            ):
                intent_kind = "foreground-dialogue"
                semantic = {
                    **semantic,
                    "requestedIntentKind": requested_intent_kind,
                    "intentKind": intent_kind,
                    "intentExecution": {
                        "status": "unsupported",
                        "capability": requested_capability,
                        "reason": f"No durable {requested_capability} executor is connected.",
                    },
                }
            elif scheduled_intents:
                semantic = {
                    **semantic,
                    "intentKind": intent_kind,
                    "intentExecution": {
                        "status": "queued",
                        "capability": "timer-delivery",
                        "accepted": scheduled_intents,
                    },
                }
            if semantic.get("memoryDraft") and intent_kind == "foreground-dialogue":
                intent_kind = "waiting-input"
            background_intent = intent_kind in {
                "background-job",
                "background-reminder",
                "persistent-task",
            }
            waiting_input_intent = intent_kind == "waiting-input"
            processing_runtime = self.session_runtime
            if background_intent:
                selected_skill = semantic.get("selectedSkill")
                processing_runtime = SkillRuntime(
                    self.registry,
                    str(selected_skill) if selected_skill else None,
                    event_sink=None,
                )
                processing_runtime.set_event_sink(
                    self.runtime_event_sink(processing_runtime)
                )
                selected_task = semantic.get("selectedTask")
                if selected_task and str(selected_task) in processing_runtime.tasks:
                    processing_runtime.select_task(str(selected_task))
            result = self.process_transcript(
                text,
                self.harness,
                processing_runtime,
                turn_id=turn_id,
                transcript_entry=work_ledger.ensure_turn_message(
                    turn_id, text, source="input-audio-transcript"
                ),
                persist_runtime_checkpoint=not background_intent,
                episodic_memory=self.current_episodic_memory(),
                user_scope=self.user_id,
                semantic_decision=semantic,
                memory_policy=self.current_memory_policy(),
                clock_context=self.clock_for_session(self.user_id, self.session_id),
            )
            if shared_scene:
                result["sharedScene"] = shared_scene
            self.resolve_semantic_turn(text, result, semantic, turn_id)
            if not background_intent:
                self.turn_state.routing = result["routingState"]
                self.owner.routing = self.turn_state.routing
                self.turn_state.episode_injection = result["episodicInjection"]
            current_skill_ref = {
                "name": processing_runtime.skill.name
                if processing_runtime.skill
                else None,
                "contentHash": processing_runtime.skill.content_hash
                if processing_runtime.skill
                else None,
                "version": processing_runtime.skill.manifest.get("version")
                if processing_runtime.skill
                else None,
                "task": processing_runtime.task.spec.id
                if processing_runtime.task
                else None,
            }
            draft_decision = semantic.get("memoryDraft") or {}
            waiting_intent = next(
                (
                    item
                    for item in reversed(list(work_ledger.intents().values()))
                    if item.get("status") == "waiting-input"
                    and (item.get("payload") or {})
                    .get("semantic", {})
                    .get("memoryDraft")
                ),
                None,
            )
            continuation_intent = (
                waiting_intent
                if str(draft_decision.get("mode", "")).casefold() == "continue"
                or (
                    pending_draft_before is not None
                    and (result.get("timelineWrite") or {}).get("consent") == "explicit"
                )
                else None
            )
            if continuation_intent is not None:
                intent = dict(continuation_intent)
                work_ledger.link_turn_to_intent(
                    intent["intentId"], turn_id, relation="memory-draft-continuation"
                )
            else:
                intent = work_ledger.create_intent(
                    turn_id,
                    kind=intent_kind,
                    payload={"semantic": semantic},
                    skill_ref=current_skill_ref,
                    status="running-background"
                    if background_intent
                    else "waiting-input"
                    if waiting_input_intent
                    else "foreground",
                    intent_id=f"intent-{turn_id}",
                )
            if background_intent:
                self.owner.intent_runtimes[intent["intentId"]] = processing_runtime
            if not background_intent:
                work_ledger.focus_intent(intent["intentId"], reason="user-turn")
            job = work_ledger.create_job(
                intent["intentId"],
                kind=intent_kind,
                payload={"turnId": turn_id, "text": text},
                status="running"
                if background_intent
                else "waiting-input"
                if waiting_input_intent or continuation_intent
                else "queued",
                job_id=f"job-{turn_id}",
            )
            if not background_intent and hasattr(self.bridge(), "set_tools"):
                self.bridge().set_tools(
                    self.session_tool_declarations(), self.session_tool_handler
                )
            if not background_intent and result.get("taskControl") == "show-reference":
                result["referenceResult"] = execute_reference_request(
                    self.harness,
                    self.execute_session_tool,
                    self.session_tool_declarations(),
                    {
                        "turnId": turn_id,
                        "intentId": intent["intentId"],
                        "jobId": job["jobId"],
                    },
                )
            if not background_intent:
                self.refresh_harness_context(
                    self.bridge(), self.harness, self.session_runtime
                )
            if not background_intent and result.get("skillControl") == "deactivate":
                self.bridge().cancel_response()
            evidence = result.get("episodicInjection", {}).get("evidence", [])
            from egoassist.product.answer_service import turn_answer_context
            shared_answer_context = turn_answer_context(text, result,
                clock=self.clock_for_session(self.user_id, self.session_id).prompt_payload(),
                runtime=self.runtime_checkpoint(processing_runtime), shared_scene=shared_scene)
            if not background_intent and hasattr(self.bridge(), "inject_context_data"):
                self.bridge().inject_context_data(
                    "current_turn_context",
                    shared_answer_context,
                )
            if result.get("control") == "stop-speaking":
                self.bridge().cancel_response(
                    drop_queued_below=101,
                    reason="stop-speaking-control"
                    if explicit
                    else "stop-speaking-transcript",
                )
                self.session_trace(
                    "user.control.stop_speaking",
                    "explicit-control" if explicit else "realtime-user",
                    {"text": text, "responseRequested": False},
                )
            if background_intent:
                work_ledger.set_intent_status(
                    intent["intentId"],
                    "running-background",
                    detail={
                        "runtimeCheckpoint": self.runtime_checkpoint(processing_runtime)
                    },
                )
                work_ledger.set_job_status(
                    job["jobId"],
                    "waiting-result",
                    detail={
                        "turnId": turn_id,
                        "runtimeCheckpoint": self.runtime_checkpoint(
                            processing_runtime
                        ),
                    },
                )
            elif waiting_input_intent or continuation_intent:
                committed = bool(
                    (result.get("timelineWrite") or {}).get("committedCardIds")
                )
                draft_still_pending = (
                    self.timeline.pending_draft(
                        user_id=self.user_id, case_id=self.session_id
                    )
                    is not None
                )
                if continuation_intent and (committed or not draft_still_pending):
                    for prior_job in work_ledger.jobs().values():
                        if (
                            prior_job.get("jobId") != job["jobId"]
                            and prior_job.get("intentId") == intent["intentId"]
                            and (prior_job.get("status") == "waiting-input")
                        ):
                            work_ledger.set_job_status(
                                prior_job["jobId"],
                                "completed",
                                detail={"continuedByTurnId": turn_id},
                            )
                    work_ledger.set_job_status(
                        job["jobId"], "completed", detail={"continuedByTurnId": turn_id}
                    )
                    work_ledger.set_intent_status(
                        intent["intentId"],
                        "completed",
                        detail={
                            "runtimeCheckpoint": self.runtime_checkpoint(
                                processing_runtime
                            ),
                            "continuedByTurnId": turn_id,
                        },
                    )
                else:
                    work_ledger.set_intent_status(
                        intent["intentId"],
                        "waiting-input",
                        detail={
                            "runtimeCheckpoint": self.runtime_checkpoint(
                                processing_runtime
                            ),
                            "memoryDraft": semantic.get("memoryDraft"),
                        },
                    )
                    work_ledger.set_job_status(
                        job["jobId"],
                        "waiting-input",
                        detail={
                            "turnId": turn_id,
                            "memoryDraft": semantic.get("memoryDraft"),
                        },
                    )
            else:
                work_ledger.set_intent_status(
                    intent["intentId"],
                    "foreground",
                    detail={
                        "runtimeCheckpoint": self.runtime_checkpoint(processing_runtime)
                    },
                )
                work_ledger.set_job_status(
                    job["jobId"],
                    "waiting-result",
                    detail={
                        "turnId": turn_id,
                        "runtimeCheckpoint": self.runtime_checkpoint(
                            processing_runtime
                        ),
                    },
                )
            primary_instruction = None
            if (
                not background_intent
                and result.get("control") != "stop-speaking"
                and (not (result.get("memoryResponse") or {}).get("requested"))
            ):
                primary_instruction = self.grounded_user_instructions(
                    selected_candidate.execution_text
                    if secondary_response_candidates
                    and selected_candidate is not None
                    and (selected_candidate.kind == "foreground-dialogue")
                    else text,
                    result,
                    self.session_runtime,
                )
                self.answer_executor.register(
                    self.user_id,
                    self.harness.journal,
                    turn_id=turn_id,
                    question=text,
                    instructions=primary_instruction,
                    context=shared_answer_context,
                    dialogue_job={"intentId": intent["intentId"], "jobId": job["jobId"]}
                    if intent_kind == "foreground-dialogue"
                    and (not processing_runtime.task)
                    and (not semantic.get("taskControl"))
                    else None,
                    deferred=turn_sequence < self.owner.latest_input_sequence,
                )
            work_ledger.set_turn_status(turn_id, "completed")
            _bg_task_queue: list[
                tuple[self.TaskSlot, str, str, SkillRuntime, dict]
            ] = []
            for _bg_cand in parallel_candidates:
                _bg_skill = str(_bg_cand.parameters.get("skill") or "") or None
                _bg_rt = SkillRuntime(self.registry, _bg_skill, event_sink=None)
                _bg_rt.set_event_sink(self.runtime_event_sink(_bg_rt))
                _bg_semantic = {
                    "method": semantic.get("method", "background-intent-fanout"),
                    "model": semantic.get("model"),
                    "status": "classified",
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
                    "selectedSkill": _bg_skill,
                    "intentKind": _bg_cand.kind,
                    "intents": [_bg_cand.model_dump()],
                    "skillStatus": "matched" if _bg_skill else "no-match",
                    "latencyMs": 0,
                    "policyVersion": semantic.get("policyVersion"),
                    "claimBoundary": "Background intent fanout; derived from compound turn classification.",
                }
                _bg_intent = work_ledger.create_intent(
                    turn_id,
                    kind=_bg_cand.kind,
                    payload={
                        "semantic": _bg_semantic,
                        "parentIntentId": intent["intentId"],
                    },
                    skill_ref={"name": _bg_skill} if _bg_skill else {},
                    status="running-background",
                    generation=self.owner.output_generation,
                )
                _bg_job = work_ledger.create_job(
                    _bg_intent["intentId"],
                    kind=_bg_cand.kind,
                    payload={
                        "text": _bg_cand.execution_text,
                        "parentIntentId": intent["intentId"],
                    },
                    status="queued",
                )
                _slot = self.TaskSlot(
                    task_id=_bg_job["jobId"],
                    kind=_bg_cand.kind,
                    text=_bg_cand.execution_text,
                    status="pending",
                    created_at=time.time(),
                    turn_id=turn_id,
                    intent_id=_bg_intent["intentId"],
                    job_id=_bg_job["jobId"],
                    delivery="display",
                )
                if len(self.owner.background_tasks) >= self._BG_TASK_LIMIT:
                    _oldest = next(iter(self.owner.background_tasks))
                    del self.owner.background_tasks[_oldest]
                self.owner.background_tasks[_slot.task_id] = _slot
                _bg_task_queue.append(
                    (_slot, _bg_cand.execution_text, _bg_cand.kind, _bg_rt, _bg_semantic)
                )
            _secondary_response_queue: list[tuple[str, dict[str, Any]]] = []
            for _candidate in secondary_response_candidates:
                _candidate_waiting = _candidate.kind == "waiting-input"
                _candidate_unsupported = _candidate.kind in self._BG_KINDS
                _candidate_kind = (
                    "waiting-input" if _candidate_waiting else "foreground-dialogue"
                )
                _child_intent = work_ledger.create_intent(
                    turn_id,
                    kind=_candidate_kind,
                    payload={
                        "semantic": {
                            **semantic,
                            "intentKind": _candidate_kind,
                            "requestedIntentKind": _candidate.kind
                            if _candidate_unsupported
                            else _candidate_kind,
                            "intentExecution": {
                                "status": "unsupported",
                                "capability": "background-execution",
                                "reason": "No durable background-execution executor is connected.",
                            }
                            if _candidate_unsupported
                            else {"status": "waiting-input"}
                            if _candidate_waiting
                            else None,
                            "intents": [_candidate.model_dump()],
                        },
                        "parentIntentId": intent["intentId"],
                    },
                    status="waiting-input" if _candidate_waiting else "pending",
                    generation=self.owner.output_generation,
                )
                _child_job = work_ledger.create_job(
                    _child_intent["intentId"],
                    kind=_candidate_kind,
                    payload={
                        "text": _candidate.execution_text,
                        "parentIntentId": intent["intentId"],
                    },
                    status="waiting-input" if _candidate_waiting else "waiting-result",
                )
                _secondary_response_queue.append(
                    (
                        _candidate.execution_text,
                        {
                            "turnId": turn_id,
                            "intentId": _child_intent["intentId"],
                            "jobId": _child_job["jobId"],
                            "skill": current_skill_ref,
                            "tools": self.session_tool_declarations(),
                            "runtimeCheckpoint": self.runtime_checkpoint(
                                processing_runtime
                            ),
                            "waitingInput": _candidate_waiting,
                            "unsupportedCapability": "background-execution"
                            if _candidate_unsupported
                            else None,
                        },
                    )
                )
            state_payload = self.state(
                self.session_runtime,
                routing_override=self.turn_state.routing,
                bridge_override=self.bridge(),
                harness_override=self.harness,
                tools_override=self.session_tools,
                episodic_override=self.current_episodic_memory(),
                media_override=self.session_media_configuration,
                user_scope=self.user_id,
                memory_policy_override=self.current_memory_policy(),
            )
        timeline_cards = (result.get("timelineWrite") or {}).get("cards") or [
            (result.get("timelineWrite") or {}).get("card") or {}
        ]
        for _slot, _cand_text, _cand_kind, _bg_rt, _bg_sem in _bg_task_queue:
            if self.owner.memory_bus:
                self.owner.memory_bus.emit_event(
                    "task_update",
                    task_id=_slot.task_id,
                    kind=_slot.kind,
                    text_preview=_slot.text[:80],
                    status="pending",
                    turn_id=turn_id,
                )
            _bg_at = asyncio.create_task(
                self._run_background_intent(
                    _slot, _cand_text, _cand_kind, _bg_rt, _bg_sem
                ),
                name=f"egoassist-bg-intent:{self.session_id}:{_slot.task_id[:8]}",
            )
            _slot.asyncio_task = _bg_at
        if any(
            (
                card.get("status") == "active" and card.get("tier") == "short"
                for card in timeline_cards
            )
        ):
            self.request_timeline_consolidation(self.owner)
        if not self.ws.closed:
            await self.ws.send_json({"type": "state", "state": state_payload})
            if explicit:
                await self.ws.send_json({"type": "interrupted"})
        memory_response = result.get("memoryResponse") or {}
        server_memory_reply = (
            memory_response.get("message") if memory_response.get("requested") else None
        )
        if (
            server_memory_reply
            and (not self.ws.closed)
            and (self.owner.socket_token is self.socket_token)
            and (self.owner.epoch == self.connection_epoch)
            and (self.owner.output_generation == turn_generation)
            and (turn_sequence == self.owner.latest_input_sequence)
        ):
            response_id = f"memory-policy-{self.connection_epoch}-{turn_sequence}"
            self.harness.journal.append_message(
                "assistant",
                server_memory_reply,
                source="server-memory-policy",
                response_id=response_id,
                idempotency_key=f"assistant:{response_id}:completed",
                metadata={
                    "purpose": "memory-policy",
                    "memoryStatus": memory_response.get("status"),
                    "committedCardIds": memory_response.get("committedCardIds", []),
                },
            )
            await self.ws.send_json(
                {
                    "type": "memory_status",
                    "turnId": turn_id,
                    "responseId": response_id,
                    "status": memory_response.get("status"),
                    "message": server_memory_reply,
                    "committedCardIds": memory_response.get("committedCardIds", []),
                }
            )
            work_ledger.set_turn_delivery(
                turn_id,
                "completed",
                responseId=response_id,
                channel="memory-receipt",
                memoryStatus=memory_response.get("status"),
            )
            self.session_trace(
                "response.served.memory_transaction",
                "server-memory-policy",
                {
                    "turnSequence": turn_sequence,
                    "status": memory_response.get("status"),
                    "reason": "server-confirmed-committed-card-ids"
                    if memory_response.get("committedCardIds")
                    else "no-committed-card-id",
                },
            )
        response_generation = turn_generation
        if control["output"] != "speech" or not self.interaction_state.allows(
            {"turnId": turn_id}
        ):
            if primary_instruction:
                self.answer_executor.interrupt(self.harness.journal, turn_id)
            if turn_sequence < self.owner.latest_input_sequence:
                self.session_trace(
                    "response.skipped.stale_user_turn",
                    "case-turn-worker",
                    {
                        "turnSequence": turn_sequence,
                        "latestInputSequence": self.owner.latest_input_sequence,
                    },
                )
            return result
        if (
            not background_intent
            and result.get("control") != "stop-speaking"
            and (not server_memory_reply)
            and (self.owner.socket_token is self.socket_token)
            and (self.owner.epoch == self.connection_epoch)
            and (not self.ws.closed)
            and (turn_sequence == self.owner.latest_input_sequence)
            and (
                not (
                    hasattr(self.bridge(), "has_unresolved_user_input")
                    and self.bridge().has_unresolved_user_input()
                )
            )
        ):
            selection = self.turn_policy(self.session_runtime).select(
                result["decision"]
            )
            await self.turn_progress(turn_id, "generating")
            await self.admit_foreground_response(
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                generation=response_generation,
                instructions=primary_instruction,
                priority=100,
                interrupt=explicit,
                speech_policy="immediate" if explicit else selection["policy"],
                reason="explicit-user-control"
                if explicit
                else f"grounded-user-turn:{selection['basis']}:{selection['action']}",
                max_protection_ms=selection["maxProtectionMs"]
                or self.turn_policy(self.session_runtime).max_protection_ms,
                origin={
                    "turnId": turn_id,
                    "answerObligation": turn_id,
                    "inputChannel": "explicit-control" if explicit else "microphone",
                    "intentId": intent["intentId"],
                    "jobId": job["jobId"],
                    "skill": current_skill_ref,
                    "tools": self.session_tool_declarations(),
                    "runtimeCheckpoint": self.runtime_checkpoint(processing_runtime),
                },
            )
            for _candidate_text, _candidate_origin in _secondary_response_queue:
                _secondary_instruction = (
                    prompt_text("run_egoassist/_secondary_instruction.1")
                    if _candidate_origin.get("unsupportedCapability")
                    else prompt_text("run_egoassist/_secondary_instruction.2")
                    if _candidate_origin.get("waitingInput")
                    else prompt_text("run_egoassist/_secondary_instruction.3")
                )
                await self.admit_foreground_response(
                    turn_id=turn_id,
                    turn_sequence=turn_sequence,
                    generation=response_generation,
                    instructions=_secondary_instruction
                    + json.dumps(_candidate_text, ensure_ascii=False),
                    priority=90,
                    interrupt=False,
                    speech_policy="immediate",
                    reason="compound-turn-additional-subtask",
                    max_protection_ms=2500,
                    output_mode="text",
                    purpose="compound-subtask",
                    origin=_candidate_origin,
                )
        elif (
            result.get("control") != "stop-speaking"
            and turn_sequence < self.owner.latest_input_sequence
        ):
            work_ledger.set_turn_delivery(
                turn_id, "interrupted", reason="newer-user-turn"
            )
            self.session_trace(
                "response.skipped.stale_user_turn",
                "case-turn-worker",
                {
                    "turnSequence": turn_sequence,
                    "latestInputSequence": self.owner.latest_input_sequence,
                },
            )
        elif (
            result.get("control") != "stop-speaking"
            and hasattr(self.bridge(), "has_unresolved_user_input")
            and self.bridge().has_unresolved_user_input()
        ):
            work_ledger.set_turn_delivery(
                turn_id, "interrupted", reason="unresolved-user-input"
            )
            self.answer_executor.interrupt(self.harness.journal, turn_id)
            self.session_trace(
                "response.skipped.unresolved_user_input",
                "case-turn-worker",
                {
                    "turnSequence": turn_sequence,
                    "latestInputSequence": self.owner.latest_input_sequence,
                },
            )
        if result.get("control") == "stop-speaking":
            work_ledger.set_turn_delivery(
                turn_id, "completed", channel="speech-control"
            )
        return result

    def resolve_semantic_turn(
        self, text: str, result: dict, semantic: dict, turn_id: str
    ) -> None:
        self.harness.journal.append(
            "control_decision",
            {
                "turnId": turn_id,
                "decision": semantic,
                "source": "semantic-turn-router",
                "input": text,
            },
        )

    async def admit_foreground_response(
        self,
        *,
        turn_id: str,
        turn_sequence: int,
        generation: int,
        instructions: str,
        priority: int,
        interrupt: bool,
        speech_policy: str,
        reason: str,
        max_protection_ms: int,
        output_mode: str = "audio",
        purpose: str = "conversation",
        origin: dict[str, Any],
    ) -> bool:
        """Atomically re-check ownership immediately before provider output."""
        async with self.owner.mutation_lock:
            if (
                self.owner.socket_token is not self.socket_token
                or self.owner.epoch != self.connection_epoch
            ):
                return False
            if generation != self.owner.output_generation:
                self.session_trace(
                    "response.skipped.stale_generation",
                    "output-admission-gate",
                    {
                        "turnId": turn_id,
                        "turnSequence": turn_sequence,
                        "generation": generation,
                        "currentGeneration": self.owner.output_generation,
                    },
                )
                return False
            if turn_sequence != self.owner.latest_input_sequence:
                self.session_trace(
                    "response.skipped.stale_user_turn",
                    "output-admission-gate",
                    {
                        "turnId": turn_id,
                        "turnSequence": turn_sequence,
                        "latestInputSequence": self.owner.latest_input_sequence,
                    },
                )
                return False
            accepted_origin = {**origin, "generation": generation, "turnId": turn_id}
            return bool(
                self.request_bridge_response(
                    self.bridge(),
                    instructions,
                    priority=priority,
                    interrupt=interrupt,
                    speech_policy=speech_policy,
                    reason=reason,
                    max_protection_ms=max_protection_ms,
                    output_mode=output_mode,
                    purpose=purpose,
                    origin=accepted_origin,
                )
            )

    async def classify_persisted_turn(self, turn_id: str, text: str) -> dict:
        ledger = self.owner.work_ledger or CaseWorkLedger(self.harness.journal)
        queued_at = time.perf_counter()
        await self.turn_progress(turn_id, "queued")
        async with self.classification_limit:
            queue_ms = round((time.perf_counter() - queued_at) * 1000, 1)
            ledger.set_turn_status(turn_id, "classifying", detail={"queueMs": queue_ms})
            await self.turn_progress(turn_id, "classifying", queueMs=queue_ms)
            turn = ledger.turns()[turn_id]
            scene_task = None
            from egoassist.core.settings import integer

            if len(self.owner.prepared_scenes) < integer(
                "EGOASSIST_VISUAL_QUESTION_CONCURRENCY", 2, maximum=8
            ):
                scene_task = asyncio.create_task(
                    self.owner.scene.context(
                        turn_id,
                        describe_frame=True,
                        question=text,
                    ),
                    name=f"egoassist-turn-scene:{turn_id}",
                )
                self.owner.prepared_scenes[turn_id] = scene_task
                self.owner.scene.tasks.add(scene_task)
                scene_task.add_done_callback(self.owner.scene.tasks.discard)
            speaker_task = asyncio.create_task(
                self.owner.scene.resolve_speaker(turn_id)
            )
            try:
                semantic = await self.classify_semantic_turn(text, turn_id)
                if semantic.get("status") in {"unavailable", "invalid"} and semantic.get("retryable", True):
                    self.harness.journal.append(
                        "interaction.classification_retry",
                        {"turnId": turn_id, "decision": semantic},
                    )
                    if not self.ws.closed:
                        await self.turn_progress(turn_id, "retrying")
                    semantic = await self.classify_semantic_turn(text, turn_id)
                if (semantic.get("providerError") or {}).get("accountBlocked"):
                    await self.emit({"type": "provider_unavailable", "lane": "router",
                                     "reason": semantic["providerError"]["message"], "retryable": False})
                scene_turn = self.owner.scene.turns.get(turn_id)
                if scene_turn and (not scene_turn["speaker"].get("wearer")):
                    from egoassist.realtime.scene import restrict_unverified_speaker

                    semantic = restrict_unverified_speaker(semantic)
                semantic["sourceScene"] = scene_turn
                if semantic.get("status") in {"unavailable", "invalid"} or any(
                    (
                        error.get("component") == "memoryAtoms"
                        for error in semantic.get("componentErrors") or []
                    )
                ):
                    source = ledger.ensure_turn_message(
                        turn_id,
                        text,
                        source=ledger.turns()[turn_id].get(
                            "source", "input-transcript"
                        ),
                    )
                    self.memory_recovery.submit(
                        self.harness.journal,
                        user_id=self.user_id,
                        turn_id=turn_id,
                        text=text,
                        source_id=source["id"],
                        clock=self.clock_for_session(
                            self.user_id, self.session_id
                        ).at_source((source.get("metadata") or {}).get("sourceRecordedAt") or source.get("timestamp")).prompt_payload(),
                        draft=self.timeline.pending_draft(
                            user_id=self.user_id, case_id=self.session_id
                        ),
                        scene=scene_turn,
                        memory_candidates=semantic.get("sourceMemoryCandidates"),
                    )
            finally:
                self.owner.scene.tasks.add(speaker_task)
                speaker_task.add_done_callback(self.owner.scene.tasks.discard)
            control_only = (semantic.get("interaction") or {}).get("action") in {
                "listen",
                "pause",
                "cancel",
                "uncertain",
            } or semantic.get("speechControl")
            if control_only and scene_task is not None:
                scene_task.cancel()
                await asyncio.gather(scene_task, return_exceptions=True)
                await self.owner.scene.visual_questions.cancel(
                    turn_id, reason="interaction-does-not-need-vision"
                )
                self.owner.prepared_scenes.pop(turn_id, None)
            from egoassist.realtime.interaction_controller import (
                apply_interaction,
                is_output_control,
            )

            control_target = (semantic.get("interaction") or {}).get("targetTurnId")
            target_available = not control_target or control_target in answer_jobs(
                self.harness.journal
            )
            if (
                is_output_control(semantic)
                and target_available
                and (turn.get("sequence") == self.owner.latest_input_sequence)
            ):
                async with self.owner.mutation_lock:
                    if (
                        self.owner.socket_token is self.socket_token
                        and self.owner.epoch == self.connection_epoch
                        and (turn.get("sequence") == self.owner.latest_input_sequence)
                    ):
                        handled, control = await apply_interaction(
                            semantic,
                            turn_id,
                            turn["sequence"],
                            owner=self.owner,
                            interaction_state=self.interaction_state,
                            harness=self.harness,
                            answer_executor=self.answer_executor,
                            user_id=self.user_id,
                            bridge=self.bridge(),
                            ws=self.ws,
                            emit=self.emit,
                            muted_response_ids=self.muted_response_ids,
                            resumed_response_ids=self.resumed_response_ids,
                            request_bridge_response=self.request_bridge_response,
                        )
                        if handled:
                            self.owner.applied_controls[turn_id] = control
                            self.resolve_semantic_turn(text, {}, semantic, turn_id)
                            ledger.set_turn_status(turn_id, "completed")
                            ledger.set_turn_delivery(
                                turn_id, "completed", channel="speech-control"
                            )
                            if not self.ws.closed:
                                await self.ws.send_json(
                                    {
                                        "type": "state",
                                        "state": self.scoped_state_for_user(
                                            self.user_id, self.session_id
                                        ),
                                    }
                                )
                            return semantic
        ledger.set_turn_status(
            turn_id, "ready", detail={"semanticMethod": semantic.get("method")}
        )
        await self.turn_progress(turn_id, "ready")
        return semantic

    async def turn_progress(self, turn_id, status, **detail):
        row = self.harness.journal.append(
            "turn.progress", {"turnId": turn_id, "phase": status, **detail}
        )
        if not self.ws.closed:
            try:
                await self.ws.send_json(
                    {
                        "type": "interaction_status",
                        "turnId": turn_id,
                        "status": status,
                        "updatedAt": row["timestamp"],
                        **detail,
                    }
                )
            except ConnectionResetError:
                # Progress is durable even if the browser closes during send.
                pass

    async def _run_background_intent(
        self,
        slot: Any,
        candidate_text: str,
        candidate_kind: str,
        bg_runtime: SkillRuntime,
        bg_semantic: dict,
    ) -> None:
        """Execute one background intent on an isolated runtime.

        Acquires the session mutation lock for safe journal/timeline writes.
        Does NOT modify live session runtime state (persist_runtime_checkpoint=False,
        dedicated bg_runtime).  Lifecycle transitions are pushed to the console
        as 'task_update' WS messages via MemoryEventBus.emit_event().
        """
        slot.status = "running"
        ledger = self.owner.work_ledger or CaseWorkLedger(self.harness.journal)
        if slot.job_id and slot.job_id in ledger.jobs():
            ledger.set_job_status(slot.job_id, "running")
        if self.owner.memory_bus:
            self.owner.memory_bus.emit_event(
                "task_update",
                task_id=slot.task_id,
                kind=slot.kind,
                text_preview=slot.text[:80],
                status="running",
            )
        try:
            timeout_seconds = max(
                1.0, float(os.getenv("EGOASSIST_BACKGROUND_TIMEOUT_SECONDS", "120"))
            )
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.process_transcript,
                    candidate_text,
                    self.harness,
                    bg_runtime,
                    turn_id=slot.turn_id,
                    transcript_entry=ledger.turn_message(str(slot.turn_id))
                    if slot.turn_id
                    else None,
                    persist_runtime_checkpoint=False,
                    episodic_memory=self.current_episodic_memory(),
                    user_scope=self.user_id,
                    semantic_decision=bg_semantic,
                    memory_policy=self.current_memory_policy(),
                    clock_context=self.clock_for_session(self.user_id, self.session_id),
                    project_retrieval=False,
                ),
                timeout=timeout_seconds,
            )
            slot.status = "done"
            slot.completed_at = time.time()
            write = result.get("timelineWrite") or {}
            slot.result_preview = (
                str((write.get("card") or {}).get("text") or write.get("id") or "")[:80]
                or None
            )
            if slot.intent_id and slot.intent_id in ledger.intents():
                result_entry = ledger.enqueue_result(
                    slot.intent_id,
                    {
                        "kind": candidate_kind,
                        "status": "completed",
                        "request": candidate_text,
                        "outcome": {
                            "routingAction": (result.get("decision") or {}).get(
                                "action"
                            ),
                            "timelineAction": write.get("action"),
                            "committedCardIds": list(
                                write.get("committedCardIds") or []
                            ),
                        },
                        "sourceTurnId": slot.turn_id,
                    },
                    job_id=slot.job_id,
                    priority=50,
                    delivery=slot.delivery,
                )
                if slot.job_id and slot.job_id in ledger.jobs():
                    ledger.set_job_status(
                        slot.job_id,
                        "completed",
                        detail={"resultId": result_entry["resultId"]},
                    )
                ledger.set_intent_status(
                    slot.intent_id,
                    "completed",
                    detail={"resultId": result_entry["resultId"]},
                )
            if self.owner.memory_bus:
                self.owner.memory_bus.emit_event(
                    "task_update",
                    task_id=slot.task_id,
                    kind=slot.kind,
                    text_preview=slot.text[:80],
                    status="done",
                    result_preview=slot.result_preview,
                    duration_ms=round((slot.completed_at - slot.created_at) * 1000),
                )
        except asyncio.CancelledError:
            slot.status = "cancelled"
            slot.completed_at = time.time()
            if slot.job_id and slot.job_id in ledger.jobs():
                ledger.set_job_status(
                    slot.job_id, "cancelled", detail={"reason": "task-cancelled"}
                )
            if slot.intent_id and slot.intent_id in ledger.intents():
                ledger.set_intent_status(
                    slot.intent_id, "cancelled", detail={"reason": "task-cancelled"}
                )
            if self.owner.memory_bus:
                self.owner.memory_bus.emit_event(
                    "task_update",
                    task_id=slot.task_id,
                    kind=slot.kind,
                    text_preview=slot.text[:80],
                    status="cancelled",
                )
            raise
        except Exception as exc:
            slot.status = "failed"
            slot.completed_at = time.time()
            if slot.job_id and slot.job_id in ledger.jobs():
                ledger.set_job_status(
                    slot.job_id, "failed", detail={"error": str(exc)[:300]}
                )
            if slot.intent_id and slot.intent_id in ledger.intents():
                ledger.set_intent_status(
                    slot.intent_id, "failed", detail={"error": str(exc)[:300]}
                )
            self.LOGGER.warning(
                "Background intent %s failed: %s", slot.task_id, exc, exc_info=True
            )
            if self.owner.memory_bus:
                self.owner.memory_bus.emit_event(
                    "task_update",
                    task_id=slot.task_id,
                    kind=slot.kind,
                    text_preview=slot.text[:80],
                    status="failed",
                    error=str(exc)[:120],
                )

    async def classify_semantic_turn(
        self, text: str, turn_id: str | None = None
    ) -> dict:
        preparation_started = time.perf_counter()
        catalog = self.registry.routing_catalog()
        turn_clock = self.clock_for_session(self.user_id, self.session_id)
        ledger = self.owner.work_ledger or CaseWorkLedger(self.harness.journal)
        source_message = ledger.turn_message(str(turn_id)) if turn_id else None
        if source_message:
            turn_clock = turn_clock.at_source(
                (source_message.get("metadata") or {}).get("sourceRecordedAt") or source_message.get("timestamp"))
        turns = ledger.turns()
        previous = None
        current_sequence = int(turns.get(str(turn_id), {}).get("sequence") or 0)
        if turn_id:
            previous = max(
                (
                    item
                    for item in turns.values()
                    if str(item.get("turnId")) != str(turn_id)
                    and int(item.get("sequence") or 0) < current_sequence
                ),
                key=lambda item: int(item.get("sequence") or 0),
                default=None,
            )
        # Supply actual writable cards even when there is no visual observation.
        policy = self.current_memory_policy()["components"]["timeline"]
        memory_candidates = [
            {**{key: card.get(key) for key in
                ("id", "kind", "text", "subject", "predicate", "scope", "sourceIds", "recordedAt")},
             "referenceId": "r" + str(index + 1)}
            for index, card in enumerate(self.timeline.cards(
                user_id=self.user_id, case_id=self.session_id,
                kinds=("fact", "state", "preference", "event"), limit=12,
            ))
            if policy["preference_enabled" if card["scope"] == "participant" else "episodic_enabled"]
            and datetime.fromisoformat(card["recordedAt"]) <= turn_clock.now_utc
        ]
        task = self.session_runtime.task.snapshot() if self.session_runtime.task else None
        vocabulary = []
        if policy["preference_enabled"]:
            for card in self.timeline.cards(user_id=self.user_id, scope="participant", limit=24):
                if datetime.fromisoformat(card["recordedAt"]) > turn_clock.now_utc:
                    continue
                if card.get("subject") and card.get("predicate"):
                    label = {"subject": card["subject"], "predicate": card["predicate"]}
                    if label not in vocabulary:
                        vocabulary.append(label)
        prior_answers = list(answer_job_summaries(self.harness.journal).values())
        self.semantic_router.set_turn_context(
            clock_context=turn_clock.prompt_payload(),
            memory_draft=self.timeline.pending_draft(
                user_id=self.user_id, case_id=self.session_id
            ),
            previous_turn=previous,
            previous_answer=next(
                (
                    {
                        "turnId": item["turnId"],
                        "text": item["question"],
                        "status": item["status"],
                    }
                    for item in reversed(
                        prior_answers
                    )
                    if item["turnId"] != turn_id
                    and item["status"] != "cancelled"
                    and (
                        int(turns.get(item["turnId"], {}).get("sequence") or 0)
                        < current_sequence
                    )
                ),
                None,
            ),
            answers=[
                {
                    "turnId": item["turnId"],
                    "text": item["question"],
                    "status": item["status"],
                }
                for item in prior_answers[-24:]
                if item["turnId"] != turn_id
                and item["status"] != "cancelled"
                and (
                    int(turns.get(item["turnId"], {}).get("sequence") or 0)
                    < current_sequence
                )
            ],
            recent_messages=[
                {
                    "role": item.get("message", {}).get("role"),
                    "text": str(item.get("message", {}).get("content", ""))[-600:],
                    "sourceId": item.get("id"),
                    "recordedAt": (item.get("metadata") or {}).get("sourceRecordedAt") or item.get("timestamp"),
                }
                for item in self.harness.journal.entries(types={'message'}, tail=6)
            ],
            playback=self.interaction_state.playback_context(),
            output_permission=self.interaction_state.snapshot(),
            scene=self.owner.scene.turns.get(turn_id),
            memory_candidates=memory_candidates,
            memory_vocabulary=vocabulary,
            active_task=project_task(task),
        )
        preparation_ms = round((time.perf_counter() - preparation_started) * 1000, 1)
        result = await self.semantic_router.classify(text, catalog)
        result.setdefault('requestMetrics', {})['preparationMs'] = preparation_ms
        result["sourceMemoryCandidates"] = memory_candidates
        return result
