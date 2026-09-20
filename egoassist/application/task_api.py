from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from egoassist.memory import ClockContext
from egoassist.product import OutcomeRecord
from egoassist.realtime import (
    CaseWorkLedger,
    SemanticTurnRouter,
    TaskSpec,
    default_memory_policy,
)
from egoassist.realtime.locale_prompts import PROMPTS


@dataclass
class TaskEndpoints:
    SKILL_CONTROL_LABELS: Any
    TASK_CONTROL_LABELS: Any
    answer_executor: Any
    append_case_runtime_checkpoint: Any
    artifacts_for_runtime: Any
    case_owner: Any
    clock_for_session: Any
    episodic_for_user: Any
    exclusive_case_transition: Any
    get_state: Any
    identity: Any
    invalidate_live_sessions: Any
    outcome_ledger: Any
    persist_tool_audit: Any
    process_transcript: Any
    queue_reminder: Any
    registry: Any
    reminder_runtime: Any
    request_timeline_consolidation: Any
    runtime_for_user: Any
    runtime_tool_declarations: Any
    scoped_episodic_memory: Any
    scoped_state_for_user: Any
    session_runtime_from: Any
    transition_case_skill: Any
    user_key: Any
    user_routing: Any
    user_tools: Any
    web: Any

    async def post_answer_control(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", ""))
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        turn_id = str(payload.get("turnId", ""))
        if payload.get("action") == "cancel":
            self.answer_executor.cancel(owner.journal, turn_id)
        elif payload.get("action") == "retry":
            self.answer_executor.retry(user_id, owner.journal, turn_id)
        else:
            raise ValueError("unknown answer action")
        return self.web.json_response(
            {"state": self.scoped_state_for_user(user_id, session_id)}
        )

    async def post_activate(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        skill_name = str(payload.get("skill", "")).strip()
        if skill_name:
            self.registry.get(skill_name)
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
            owner = self.case_owner(user_id, session_id)
            async with self.exclusive_case_transition(owner):
                active_routing = self.transition_case_skill(
                    owner.journal,
                    owner.runtime,
                    skill_name or None,
                    source="research-console",
                )
                owner.routing = active_routing
            return self.web.json_response(
                self.scoped_state_for_user(
                    user_id,
                    session_id,
                    runtime_override=owner.runtime,
                    routing_override=active_routing,
                )
            )
        active_runtime = self.runtime_for_user(user_id)
        await self.invalidate_live_sessions(user_id)
        if not skill_name:
            active_runtime.deactivate()
        elif not active_runtime.skill or active_runtime.skill.name != skill_name:
            active_runtime.activate(skill_name, select_default=False)
        self.user_routing[user_id] = self.registry.route("")
        return await self.get_state(request)

    async def post_task_select(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        task_id = str(payload.get("task", ""))
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
            owner = self.case_owner(user_id, session_id)
            journal, active_runtime = (owner.journal, owner.runtime)
        else:
            owner = None
            journal = None
            active_runtime = self.runtime_for_user(user_id)
        if owner:
            async with self.exclusive_case_transition(owner):
                if task_id not in active_runtime.tasks:
                    raise ValueError(
                        f"unknown task for {(active_runtime.skill.name if active_runtime.skill else 'general mode')}: {task_id}"
                    )
                active_runtime.select_task(task_id)
                self.append_case_runtime_checkpoint(
                    journal,
                    active_runtime,
                    event_type="runtime.task.selected",
                    source="research-console",
                    payload={"task": task_id},
                    reason="task-selected:research-console",
                )
        else:
            if task_id not in active_runtime.tasks:
                raise ValueError(
                    f"unknown task for {(active_runtime.skill.name if active_runtime.skill else 'general mode')}: {task_id}"
                )
            await self.invalidate_live_sessions(user_id)
            active_runtime.select_task(task_id)
        if journal:
            return self.web.json_response(
                self.scoped_state_for_user(
                    user_id, session_id, runtime_override=active_runtime
                )
            )
        return await self.get_state(request)

    async def post_task_create(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
            owner = self.case_owner(user_id, session_id)
            journal, active_runtime = (owner.journal, owner.runtime)
        else:
            owner = None
            journal = None
            active_runtime = self.runtime_for_user(user_id)
        title = str(payload.get("title", "")).strip()
        raw_steps = payload.get("steps", [])
        if not title or not isinstance(raw_steps, list) or (not raw_steps):
            raise ValueError("custom task requires a title and at least one step")
        steps = []
        for index, value in enumerate(raw_steps):
            if isinstance(value, str):
                instruction = value.strip()
                step = {
                    "id": f"step-{index + 1}",
                    "title": PROMPTS.step_title.format(n=index + 1),
                    "instruction": instruction,
                    "completion": PROMPTS.step_completion.format(
                        instruction=instruction
                    ),
                    "risk": "medium",
                    "source": "user-defined",
                }
            elif isinstance(value, dict):
                step = value
            else:
                raise ValueError("custom task steps must be text or structured objects")
            steps.append(step)

        def build_spec() -> TaskSpec:
            if active_runtime.skill is None:
                raise ValueError("activate a domain skill before creating a task")
            return TaskSpec.from_dict(
                {
                    "id": f"custom-{uuid4().hex[:8]}",
                    "title": title,
                    "version": "user-1",
                    "role": f"{active_runtime.skill.name} {PROMPTS.role_suffix}",
                    "steps": steps,
                    "safety_policy": [
                        PROMPTS.safety_policy_domain,
                        PROMPTS.safety_policy_uncertainty,
                    ],
                },
                provenance="user-defined",
            )

        if owner:
            async with self.exclusive_case_transition(owner):
                spec = build_spec()
                active_runtime.create_task(spec)
                self.append_case_runtime_checkpoint(
                    journal,
                    active_runtime,
                    event_type="runtime.task.created",
                    source="research-console",
                    payload={"task": spec.id, "provenance": spec.provenance},
                    reason="task-created:research-console",
                )
        else:
            spec = build_spec()
            await self.invalidate_live_sessions(user_id)
            active_runtime.create_task(spec)
        if journal:
            return self.web.json_response(
                self.scoped_state_for_user(
                    user_id, session_id, runtime_override=active_runtime
                )
            )
        return await self.get_state(request)

    async def post_voice(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
            owner = self.case_owner(user_id, session_id)
            active_runtime = owner.runtime
            harness = owner.harness
            policy = self.identity.get_context_session(user_id, session_id)[
                "memoryPolicy"
            ]
            active_episodic = self.scoped_episodic_memory(user_id, session_id, policy)
        else:
            owner = None
            harness = None
            policy = default_memory_policy()
            active_runtime = self.runtime_for_user(user_id)
            active_episodic = self.episodic_for_user(user_id)
        transcript = str(payload.get("transcript", ""))
        labels = {
            "speechControl": payload.get("speechControl"),
            "taskControl": payload.get("taskControl"),
            "preferenceIntent": payload.get("preferenceIntent"),
            "memoryQuery": payload.get("memoryQuery"),
            "memoryKind": payload.get("memoryKind"),
            "memoryText": payload.get("memoryText"),
            "memoryKey": payload.get("memoryKey"),
            "memoryScope": payload.get("memoryScope"),
            "memoryConsent": payload.get("memoryConsent"),
            "skillControl": payload.get("skillControl"),
        }
        memory_atoms = SemanticTurnRouter._normalize_atoms(
            payload.get("memoryAtoms", [])
        )
        memory_draft = SemanticTurnRouter._normalize_draft(payload.get("memoryDraft"))
        memory_query_spec = SemanticTurnRouter._normalize_query_spec(
            payload.get("memoryQuerySpec")
        )
        if labels["speechControl"] not in {None, "stop-speaking"}:
            raise ValueError("invalid speechControl research label")
        if labels["taskControl"] not in self.TASK_CONTROL_LABELS:
            raise ValueError("invalid taskControl research label")
        if labels["preferenceIntent"] not in {None, "propose-durable"}:
            raise ValueError("invalid preferenceIntent research label")
        if labels["memoryQuery"] not in {None, "referential"}:
            raise ValueError("invalid memoryQuery research label")
        if labels["memoryKind"] not in {
            None,
            "capture",
            "fact",
            "event",
            "state",
            "preference",
        }:
            raise ValueError("invalid memoryKind research label")
        if (
            labels["memoryKind"]
            and labels["memoryKind"] != "capture"
            and (not labels["memoryText"])
        ):
            raise ValueError("memoryText is required for a research memory card")
        if labels["memoryScope"] not in {None, "case", "participant"}:
            raise ValueError("invalid memoryScope research label")
        if labels["memoryConsent"] not in {None, "observed", "explicit", "inferred"}:
            raise ValueError("invalid memoryConsent research label")
        if labels["skillControl"] not in self.SKILL_CONTROL_LABELS:
            raise ValueError("invalid skillControl research label")
        deterministic_route = self.registry.route(transcript)
        selected_skill = (
            None
            if labels["skillControl"] == "deactivate"
            else deterministic_route.get("selectedSkill")
        )
        api_semantic = {
            "method": "explicit-research-api-labels-v1",
            "model": None,
            "status": "provided",
            **labels,
            "memoryAtoms": memory_atoms,
            "memoryDraft": memory_draft,
            "memoryQuerySpec": memory_query_spec,
            "selectedSkill": selected_skill,
            "skillStatus": "deactivated"
            if labels["skillControl"] == "deactivate"
            else deterministic_route.get("status", "no-match"),
            "claimBoundary": "Caller-supplied research labels plus deterministic manifest routing; no language model.",
        }
        if owner:
            async with self.exclusive_case_transition(owner):
                result = self.process_transcript(
                    transcript,
                    harness,
                    active_runtime,
                    episodic_memory=active_episodic,
                    user_scope=user_id,
                    semantic_decision=api_semantic,
                    memory_policy=policy,
                    clock_context=self.clock_for_session(user_id, session_id),
                )
                owner.routing = result["routingState"]
            timeline_cards = (result.get("timelineWrite") or {}).get("cards") or [
                (result.get("timelineWrite") or {}).get("card") or {}
            ]
            if any(
                (
                    card.get("status") == "active" and card.get("tier") == "short"
                    for card in timeline_cards
                )
            ):
                self.request_timeline_consolidation(owner)
        else:
            result = self.process_transcript(
                transcript,
                harness,
                active_runtime,
                episodic_memory=active_episodic,
                user_scope=user_id,
                semantic_decision=api_semantic,
                memory_policy=policy,
                clock_context=ClockContext.current(),
            )
        if session_id:
            return self.web.json_response(
                self.scoped_state_for_user(
                    user_id,
                    session_id,
                    runtime_override=active_runtime,
                    routing_override=result["routingState"],
                )
            )
        self.user_routing[user_id] = result["routingState"]
        return await self.get_state(request)

    async def post_reset(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json() if request.can_read_body else {}
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
            owner = self.case_owner(user_id, session_id)
            async with self.exclusive_case_transition(owner):
                active_routing = self.transition_case_skill(
                    owner.journal, owner.runtime, None, source="research-console-reset"
                )
                owner.routing = active_routing
                (owner.work_ledger or CaseWorkLedger(owner.journal)).focus_intent(
                    None, reason="research-console-reset"
                )
            return self.web.json_response(
                self.scoped_state_for_user(
                    user_id,
                    session_id,
                    runtime_override=owner.runtime,
                    routing_override=active_routing,
                )
            )
        await self.invalidate_live_sessions(user_id)
        self.runtime_for_user(user_id).deactivate()
        self.user_routing[user_id] = self.registry.route("")
        return await self.get_state(request)

    async def post_intent_focus(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip()
        intent_id = str(payload.get("intentId", "")).strip()
        if not session_id or not intent_id:
            raise ValueError("sessionId and intentId are required")
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
        snapshot = ledger.intent_runtime_snapshot(intent_id)
        if snapshot is None:
            raise ValueError("intent has no restorable runtime checkpoint")
        async with self.exclusive_case_transition(owner):
            owner.runtime = self.session_runtime_from(
                owner.journal, checkpoint_override=snapshot
            )
            ledger.focus_intent(intent_id, reason="explicit-resume")
            ledger.set_intent_status(
                intent_id, "foreground", detail={"runtimeCheckpoint": snapshot}
            )
            owner.journal.append_state_checkpoint(
                snapshot, reason=f"intent-resumed:{intent_id}"
            )
            owner.routing = self.registry.route("")
        state_payload = self.scoped_state_for_user(
            user_id,
            session_id,
            runtime_override=owner.runtime,
            routing_override=owner.routing,
        )
        active_ws = owner.socket
        if active_ws is not None and (not active_ws.closed):
            try:
                await active_ws.send_json({"type": "state", "state": state_payload})
            except Exception:
                pass
        return self.web.json_response(state_payload)

    async def post_job_cancel(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip()
        job_id = str(payload.get("jobId", "")).strip()
        if not session_id or not job_id:
            raise ValueError("sessionId and jobId are required")
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
        existing = ledger.jobs().get(job_id)
        if existing is None:
            raise ValueError("job not found")
        workflow_id = str((existing.get("payload") or {}).get("workflowId", ""))
        if (
            workflow_id
            and existing.get("status") not in {"completed", "failed", "cancelled"}
            and (self.reminder_runtime.get("coordinator") is not None)
        ):
            await self.reminder_runtime["coordinator"].cancel(workflow_id)
        async with owner.mutation_lock:
            job = ledger.cancel_job(job_id)
        return self.web.json_response(
            {
                "job": job,
                "state": self.scoped_state_for_user(
                    user_id, session_id, runtime_override=owner.runtime
                ),
            }
        )

    async def post_reminder_create(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip()
        if not session_id:
            raise ValueError("sessionId is required")
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        async with owner.mutation_lock:
            result = self.queue_reminder(
                owner,
                seconds=payload.get("seconds"),
                label=payload.get("label", "Reminder"),
            )
        return self.web.json_response(
            {
                "reminder": result,
                "state": self.scoped_state_for_user(
                    user_id, session_id, runtime_override=owner.runtime
                ),
            }
        )

    async def post_result_dismiss(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip()
        result_id = str(payload.get("resultId", "")).strip()
        if not session_id or not result_id:
            raise ValueError("sessionId and resultId are required")
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
        async with owner.mutation_lock:
            result = ledger.results().get(result_id)
            if result is None:
                raise ValueError("result not found")
            if result.get("status") not in {"delivered", "dismissed"}:
                ledger.set_result_status(
                    result_id, "dismissed", detail={"reason": "explicit-user-dismiss"}
                )
            self.outcome_ledger.record(
                OutcomeRecord(
                    outcome_id=f"ack:{result_id}",
                    kind="result-delivery",
                    expected="dismissed",
                    observed="dismissed",
                    source="user-acknowledgement",
                    acknowledged=True,
                    user_id=user_id,
                    session_id=session_id,
                )
            )
            result = ledger.results()[result_id]
        return self.web.json_response(
            {
                "result": result,
                "state": self.scoped_state_for_user(
                    user_id, session_id, runtime_override=owner.runtime
                ),
            }
        )

    async def post_tool(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
            owner = self.case_owner(user_id, session_id)
            active_runtime = owner.runtime
            active_tools = owner.tools
        else:
            owner = None
            active_runtime = self.runtime_for_user(user_id)
            active_tools = self.user_tools[user_id]
        if owner:
            async with owner.mutation_lock:
                if active_runtime.skill is None:
                    raise ValueError("no active skill exposes tools")
                audit_count = len(active_tools.audit)
                tool_name = str(payload.get("tool", ""))
                arguments = payload.get("arguments", {})
                runtime_declarations = self.runtime_tool_declarations(
                    active_runtime.skill
                )
                declaration, arguments, blocked = active_tools.prepare(
                    tool_name, arguments, runtime_declarations, True
                )
                if blocked is not None:
                    result = blocked
                elif (
                    tool_name == "set_timer"
                    and declaration
                    and (declaration.get("handler") == "builtin.temporal-reminder.v1")
                ):
                    result = self.queue_reminder(
                        owner,
                        seconds=arguments.get("seconds"),
                        label=arguments.get("label", "Reminder"),
                    )
                    active_tools.audit.append(
                        {
                            "time": datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            ),
                            "tool": tool_name,
                            "handler": "builtin.temporal-reminder.v1",
                            "arguments": dict(arguments),
                            "result": dict(result),
                        }
                    )
                elif (
                    tool_name == "show_reference"
                    and declaration
                    and (declaration.get("handler") == "builtin.artifact-reference.v1")
                ):
                    available = self.artifacts_for_runtime(active_runtime, user_id)
                    if not available:
                        raise ValueError(
                            "no artifact is authorized for the current checkpoint"
                        )
                    result = {"status": "resolved", "artifact": available[0]}
                    active_tools.audit.append(
                        {
                            "time": datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            ),
                            "tool": tool_name,
                            "handler": "builtin.artifact-reference.v1",
                            "arguments": dict(arguments),
                            "result": dict(result),
                        }
                    )
                else:
                    result = active_tools.execute(
                        tool=tool_name,
                        arguments=arguments,
                        declared_tools=runtime_declarations,
                        confirmed=True,
                    )
                self.persist_tool_audit(owner, audit_count)
        else:
            if active_runtime.skill is None:
                raise ValueError("no active skill exposes tools")
            result = active_tools.execute(
                tool=str(payload.get("tool", "")),
                arguments=payload.get("arguments", {}),
                declared_tools=active_runtime.skill.manifest.get("tools", []),
                confirmed=True,
            )
        state_payload = self.scoped_state_for_user(
            user_id,
            session_id,
            runtime_override=active_runtime,
            tools_override=active_tools,
        )
        return self.web.json_response({"result": result, "state": state_payload})
