#!/usr/bin/env python3
"""EgoAssist full-duplex multimodal framework entry point."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import webbrowser
from pathlib import Path
from uuid import uuid4

from egoassist import BUILTIN_SKILLS_ROOT, CONSOLE_ROOT
from egoassist._env import load_env
from egoassist.realtime import (
    PreferenceMemory,
    RealtimeEpisodeMemory,
    EventLedger,
    SkillRegistry,
    SkillRuntime,
    SkillToolExecutor,
    TaskSpec,
    TurnTakingPolicy,
)


ROOT = Path(__file__).resolve().parent
WEB_ROOT = CONSOLE_ROOT
LOGGER = logging.getLogger("egoassist.realtime")


def runtime_root() -> Path:
    """Resolve user-owned runtime state consistently for source and wheel launches."""
    working_root = Path.cwd()
    if (working_root / ".env").is_file():
        return working_root
    if (ROOT / ".env").is_file():
        return ROOT
    return working_root


def create_app(skills_path: str, active_skill: str | None, memory_path: str, model: str, voice: str):
    from aiohttp import WSMsgType, web

    from egoassist.realtime.omni import OmniBrowserBridge

    registry = SkillRegistry(skills_path)
    ledger = EventLedger()
    runtime_context = {"skill": active_skill or None, "task": None}
    episodic = None

    def trace_event(event_type, source, payload, parent_event_ids=None, **metadata):
        event = ledger.append(
            event_type, source, payload, parent_event_ids=parent_event_ids or [],
            model=metadata.get("model", model), skill=runtime_context["skill"],
            task=runtime_context["task"], response_id=metadata.get("response_id"),
            latency_ms=metadata.get("latency_ms"),
        )
        if episodic and event_type == "proactive.gate.decision":
            has_scene_anchor = any(item["kind"] == "scene" for item in episodic.snapshot()["recent"])
            if payload.get("salientChange") or not has_scene_anchor:
                episodic.remember(
                    payload.get("sceneSummary", ""),
                    kind="scene",
                    source="proactive-visual-gate",
                    skill=runtime_context["skill"],
                    task=runtime_context["task"],
                    evidence_event_id=event["eventId"],
                )
        return event

    runtime = SkillRuntime(registry, active_skill, event_sink=trace_event)
    if runtime.active:
        runtime_context.update(
            skill=runtime.skill.name,
            task=runtime.task.spec.id if runtime.task else None,
        )
    memory = PreferenceMemory(memory_path, event_sink=trace_event)
    episodic = RealtimeEpisodeMemory(Path(memory_path).with_name("episodes.json"), event_sink=trace_event)
    tools = SkillToolExecutor()
    live_bridges: set[OmniBrowserBridge] = set()
    live_sockets: set = set()
    routing_state = registry.route("")
    media_configuration: dict = {}

    def context() -> str:
        return runtime.context(memory.snapshot()["records"])

    def memory_retrieval(*, emit: bool = False) -> dict:
        if runtime.task is None:
            return {
                "method": "exact-trigger-v1",
                "query": {"task": None, "checkpoint": None},
                "selected": None,
                "candidates": [],
            }
        task = runtime.task
        spec = task.spec
        attempts = [
            memory.retrieve(spec.id, task.current_step.id, emit=emit),
            memory.retrieve(spec.title, task.current_step.title, emit=False),
        ]
        return next((item for item in attempts if item["selected"]), attempts[0])

    def find_memory_hint(memory_state: dict | None = None, *, emit: bool = False) -> dict | None:
        del memory_state
        selected = memory_retrieval(emit=emit).get("selected")
        return selected["record"] if selected else None

    def apply_memory_hint() -> tuple[dict | None, bool]:
        if runtime.task is None:
            return None, False
        hint = find_memory_hint(emit=True)
        already_used = bool(hint and hint["id"] in runtime.task.announced_memory_ids)
        runtime.task.apply_memory_hint(hint)
        return hint, bool(hint and not already_used)

    def state():
        memory_state = memory.snapshot()
        compiled_context = context()
        bridges = tuple(live_bridges)
        scheduler = bridges[0].scheduler_snapshot() if bridges and hasattr(bridges[0], "scheduler_snapshot") else {
            "pending": False, "activeResponseIds": [], "queued": None, "inflight": None,
            "activePolicies": [], "bargeInPending": False,
        }
        default_policy = TurnTakingPolicy()
        audio_attribution = {
            "input": "provider input_audio_transcription -> user",
            "output": "provider audio_transcript -> assistant",
            "browserCapture": media_configuration,
            "echoControl": "browser-webrtc-aec",
            "claimBoundary": "Logical channels are distinct; residual acoustic echo is observable but speaker identity is not guaranteed.",
        }
        episodic_state = episodic.snapshot()
        if runtime.task is None:
            candidate = memory_state.get("candidate")
            generic_memory = {
                "policy": "explicit-confirmation",
                "scope": {"skill": runtime.skill.name if runtime.skill else None, "task": None, "contextAnchor": None},
                "evidence": {"status": "waiting", "text": "等待实时对话或感知证据", "source": "realtime-session"},
                "candidate": {"status": "pending" if candidate else "none", "text": candidate.get("evidence") if candidate else "没有待确认候选"},
                "confirmation": {"status": "none", "text": "尚无活动任务范围", "count": 0},
                "retrieval": {"status": "no-match", "text": "确定任务后才执行精确偏好召回", **memory_retrieval()},
                "impact": {"status": "none", "text": "不改变当前决策"},
                "lastDecision": memory_state["decision"],
            }
            generic_decision = {
                "action": "ask" if runtime.skill else "silent",
                "message": "等待确认具体目标" if runtime.skill else "通用观察与对话",
                "reason": "skill-loaded-task-unresolved" if runtime.skill else "no-domain-intent",
                "confidence": 1.0 if runtime.skill else 0.0,
            }
            return {
                "memory": memory_state,
                "memoryMechanism": generic_memory,
                "task": None,
                "genericDecision": generic_decision,
                "realtimeModel": model,
                "activeSkill": runtime.skill.public_summary() if runtime.skill else None,
                "skills": registry.summaries(),
                "tasks": runtime.task_summaries(),
                "intentRouting": routing_state,
                "audioAttribution": audio_attribution,
                "episodicMemory": episodic_state,
                "toolAudit": list(reversed(tools.audit[-8:])),
                "events": ledger.snapshot(),
                "sessionId": ledger.session_id,
                "scheduler": scheduler,
                "turnTakingPolicy": {
                    **default_policy.public(),
                    "currentSelection": default_policy.select(generic_decision),
                    "source": "core-default",
                },
                "memorySystems": [
                    {"id": "working", "name": "工作记忆", "status": "live" if bridges else "idle", "implementation": "Realtime 会话上下文与最近结构化事件"},
                    {"id": "preference", "name": "偏好记忆", "status": "waiting-scope", "implementation": "任务确定后使用用户确认的精确触发"},
                    {"id": "episodic", "name": "情景记忆", "status": "connected-summary", "implementation": "实时场景/话语摘要索引；不保存原始媒体"},
                    {"id": "knowledge", "name": "知识记忆", "status": "unloaded", "implementation": "领域 Skill 尚未加载"},
                ],
                "knowledgeMechanism": {
                    "method": "none",
                    "selected": None,
                    "candidates": [],
                    "claimBoundary": "No domain knowledge is injected before intent routing.",
                },
                "promptContext": {
                    "hash": hashlib.sha256(compiled_context.encode("utf-8")).hexdigest()[:16],
                    "text": compiled_context,
                    "skill": runtime.skill.name if runtime.skill else None,
                    "task": None,
                    "taskVersion": None,
                },
            }
        task = runtime.task
        spec = runtime.task.spec
        task_state = task.snapshot()
        retrieval = memory_retrieval()
        hint = retrieval["selected"]["record"] if retrieval["selected"] else None
        hint_was_used = bool(hint and hint["id"] in task.announced_memory_ids)
        task_records = [
            record for record in memory_state["records"]
            if record.get("trigger", {}).get("task") in {spec.id, spec.title}
        ]
        observation = task_state.get("observation") or {}
        candidate = memory_state.get("candidate")
        memory_mechanism = {
            "policy": "explicit-confirmation",
            "scope": {
                "skill": runtime.skill.name,
                "task": spec.id,
                "contextAnchor": task.current_step.id,
            },
            "evidence": {
                "status": "observed" if observation else "waiting",
                "text": observation.get("summary") or "等待实时对话或感知证据",
                "source": observation.get("source") or "realtime-session",
            },
            "candidate": {
                "status": "pending" if candidate else "none",
                "text": candidate.get("evidence") if candidate else "没有待确认候选",
            },
            "confirmation": {
                "status": "confirmed" if task_records else "none",
                "text": f"当前任务有 {len(task_records)} 条用户确认记忆",
                "count": len(task_records),
            },
            "retrieval": {
                "status": "matched" if hint else "no-match",
                "text": hint.get("prompt") if hint else "当前情境没有匹配记忆",
                "memoryId": hint.get("id") if hint else None,
                "method": retrieval["method"],
                "query": retrieval["query"],
                "candidates": retrieval["candidates"],
            },
            "impact": {
                "status": "reused" if hint_was_used else "ready" if hint else "none",
                "text": (
                    "已注入本会话，后续避免重复打扰"
                    if hint_was_used
                    else "等待实时事件触发语音提醒"
                    if hint
                    else "不改变当前决策"
                ),
            },
            "lastDecision": memory_state["decision"],
        }
        return {
            "memory": memory_state,
            "memoryMechanism": memory_mechanism,
            "task": task_state,
            "realtimeModel": model,
            "activeSkill": runtime.skill.public_summary(),
            "skills": registry.summaries(),
            "tasks": runtime.task_summaries(),
            "intentRouting": routing_state,
            "audioAttribution": audio_attribution,
            "episodicMemory": episodic_state,
            "toolAudit": list(reversed(tools.audit[-8:])),
            "events": ledger.snapshot(),
            "sessionId": ledger.session_id,
            "scheduler": scheduler,
            "turnTakingPolicy": {
                **TurnTakingPolicy.from_manifest(runtime.skill.manifest).public(),
                "currentSelection": TurnTakingPolicy.from_manifest(runtime.skill.manifest).select(task.decision),
                "source": f"skill:{runtime.skill.name}",
            },
            "memorySystems": [
                {"id": "working", "name": "工作记忆", "status": "live" if bridges else "idle", "implementation": "Realtime 会话上下文与最近结构化观测"},
                {"id": "preference", "name": "偏好记忆", "status": "connected", "implementation": "用户确认 JSON + 精确触发"},
                {"id": "episodic", "name": "情景记忆", "status": "connected-summary", "implementation": "实时场景/话语摘要索引；层次视觉后端仍为可替换离线实现"},
                {"id": "knowledge", "name": "知识记忆", "status": "static-injection", "implementation": "Skill/Task 静态上下文，尚无运行时语义检索"},
            ],
            "knowledgeMechanism": {
                "method": "static-skill-task-injection-v1",
                "selected": {"checkpoint": task.current_step.id, "source": task.current_step.source},
                "candidates": [{"checkpoint": item.id, "source": item.source, "selected": item.id == task.current_step.id} for item in spec.steps],
                "claimBoundary": "不是语义知识检索；references/ 当前未被运行时查询",
            },
            "promptContext": {
                "hash": hashlib.sha256(compiled_context.encode("utf-8")).hexdigest()[:16],
                "text": compiled_context,
                "skill": runtime.skill.name,
                "task": spec.id,
                "taskVersion": spec.version,
            },
        }

    def refresh_live_context() -> None:
        for bridge in tuple(live_bridges):
            try:
                bridge.update_context(context())
            except Exception:
                live_bridges.discard(bridge)

    def turn_policy() -> TurnTakingPolicy:
        return TurnTakingPolicy.from_manifest(runtime.skill.manifest) if runtime.skill else TurnTakingPolicy()

    def request_bridge_response(
        bridge, instructions, *, priority, interrupt=False,
        speech_policy="immediate", reason="unspecified", max_protection_ms=2500,
    ):
        if hasattr(bridge, "scheduler_snapshot"):
            return bridge.request_response(
                instructions, priority=priority, interrupt=interrupt,
                speech_policy=speech_policy, reason=reason,
                max_protection_ms=max_protection_ms,
            )
        return bridge.request_response(instructions, priority=priority, interrupt=interrupt)

    async def invalidate_live_sessions() -> None:
        sockets = tuple(live_sockets)
        for socket in sockets:
            if not socket.closed:
                await socket.send_json({"type": "session_stale"})
                await socket.close(code=1000, message=b"task context changed")

    def control_instructions(transcript: str) -> str:
        text = transcript.casefold()
        if runtime.task is None:
            if runtime.skill:
                return (
                    f"用户说：{transcript[:160]}。当前已加载 {runtime.skill.name}，但具体任务尚未确定。"
                    "自然回应并只询问一个确定目标所必需的问题；不要套用默认任务。"
                )
            return (
                f"用户说：{transcript[:160]}。以通用实时多模态助手身份自然回应。"
                "当前没有领域 Skill；不要虚构任务、步骤或内部决策。"
            )
        decision = runtime.task.decision
        if any(word in text for word in ("重复", "再说", "repeat", "没听清")):
            return (
                "用户请求重复上一条实际说出的指导。结合当前画面和当前任务状态，用更短、更清楚的一句话重述；"
                "不要退回照读步骤文本，不要假设步骤已完成。"
            )
        if any(word in text for word in ("帮助", "怎么做", "下一步", "help")):
            return (
                "用户现在需要帮助。结合当前画面、当前步骤、已确认进度和安全证据，只给一个现在可执行的动作；"
                "若画面不足以判断，先问一个最关键的澄清问题。"
            )
        if any(word in text for word in ("完成了", "已经好了", "这步好了", "可以下一步了", "done")):
            return (
                f"用户已明确确认上一步完成。简短确认，然后依据当前画面给当前新步骤的一个可执行动作。"
                f"结构化控制面当前为：{decision['message']}"
            )
        if any(word in text for word in ("暂停", "停止", "stop", "pause")):
            return "用户要求暂停。只简短确认已暂停，不再给下一步动作。"
        if any(word in text for word in ("继续", "恢复", "resume")):
            return "用户恢复任务。结合当前画面和当前步骤，只给一个现在可执行的动作。"
        return (
            f"响应用户刚才的控制请求：{transcript[:160]}。结合当前画面与任务状态自然应答；"
            "不要假设不可见事实，也不要一次给多个动作。"
        )

    def request_decision_speech(decision: dict, priority: int | None = None) -> None:
        if decision.get("action") not in {"guide", "ask", "emergency"}:
            return
        request_priority = priority if priority is not None else (100 if decision["action"] == "emergency" else 70)
        instructions = (
            f"结构化感知或控制面刚产生了需要发声的决策：{decision['message']}；依据：{decision['reason']}。"
            "结合当前画面核对后自然地说出必要内容。风险必须立即明确提示；非风险只给一个动作或一个问题。"
        )
        for bridge in tuple(live_bridges):
            bridge.update_context(context())
            selection = turn_policy().select(decision)
            request_bridge_response(
                bridge, instructions, priority=request_priority,
                interrupt=decision["action"] == "emergency", speech_policy=selection["policy"],
                reason=f"{selection['basis']}:{decision['action']}",
                max_protection_ms=selection["maxProtectionMs"] or turn_policy().max_protection_ms,
            )

    def request_memory_speech(hint: dict | None) -> None:
        if not hint:
            return
        instructions = (
            f"刚刚召回一条用户明确确认的长期记忆：{hint['prompt']}。"
            "结合当前画面自然地给出一句简短语音提醒；不要提及数据库、记忆编号或内部步骤。"
        )
        for bridge in tuple(live_bridges):
            bridge.update_context(context())
            request_bridge_response(bridge, instructions, priority=80, speech_policy="immediate", reason="preference-memory-recall")

    async def get_state(request):
        return web.json_response(state())

    async def get_index(request):
        return web.FileResponse(WEB_ROOT / "index.html")

    async def post_activate(request):
        payload = await request.json()
        skill_name = str(payload.get("skill", "")).strip()
        await invalidate_live_sessions()
        if not skill_name:
            runtime.deactivate()
            runtime_context.update(skill=None, task=None)
            trace_event("runtime.skill.deactivated", "control-api", {"reason": "general-mode"})
        else:
            registry.get(skill_name)
            runtime.activate(skill_name, select_default=False)
            runtime_context.update(skill=runtime.skill.name, task=None)
            trace_event("runtime.skill.activated", "control-api", {"skill": runtime.skill.name, "task": None})
        return web.json_response(state())

    async def post_task_select(request):
        task_id = str((await request.json()).get("task", ""))
        if task_id not in runtime.tasks:
            raise ValueError(f"unknown task for {runtime.skill.name if runtime.skill else 'general mode'}: {task_id}")
        await invalidate_live_sessions()
        runtime.select_task(task_id)
        runtime_context["task"] = runtime.task.spec.id
        trace_event("runtime.task.selected", "control-api", {"task": runtime.task.spec.id})
        return web.json_response(state())

    async def post_task_create(request):
        if runtime.skill is None:
            raise ValueError("activate a domain skill before creating a task")
        payload = await request.json()
        title = str(payload.get("title", "")).strip()
        raw_steps = payload.get("steps", [])
        if not title or not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("custom task requires a title and at least one step")
        steps = []
        for index, value in enumerate(raw_steps):
            if isinstance(value, str):
                instruction = value.strip()
                step = {
                    "id": f"step-{index + 1}", "title": f"步骤 {index + 1}",
                    "instruction": instruction,
                    "completion": f"用户确认已完成：{instruction}",
                    "risk": "medium", "source": "user-defined",
                }
            elif isinstance(value, dict):
                step = value
            else:
                raise ValueError("custom task steps must be text or structured objects")
            steps.append(step)
        spec = TaskSpec.from_dict({
            "id": f"custom-{uuid4().hex[:8]}", "title": title, "version": "user-1",
            "role": f"{runtime.skill.name} 实时辅助助手", "steps": steps,
            "safety_policy": ["遵守当前 Skill 的领域安全边界", "不确定时询问，不根据单帧自动推进"],
        }, provenance="user-defined")
        await invalidate_live_sessions()
        runtime.create_task(spec)
        runtime_context["task"] = runtime.task.spec.id
        trace_event("runtime.task.created", "control-api", {"task": spec.id, "title": spec.title, "provenance": spec.provenance})
        return web.json_response(state())

    def process_transcript(transcript: str) -> dict:
        nonlocal routing_state
        transcript_event = trace_event("audio.transcript.completed", "realtime-user", {"text": transcript})
        routing_state = registry.route(transcript)
        route_event = trace_event(
            "intent.route.decision", "skill-router", routing_state,
            [transcript_event["eventId"]],
        )
        normalized = transcript.casefold()
        explicit_switch = any(word in normalized for word in ("切换到", "改做", "开始做", "我要做", "switch to"))
        selected_skill = routing_state.get("selectedSkill")
        selected_task = routing_state.get("selectedTask")
        runtime_changed = False
        matched_task = None
        if selected_skill and (not runtime.active or explicit_switch or runtime.task is None):
            if not runtime.skill or runtime.skill.name != selected_skill:
                runtime.activate(selected_skill, select_default=False)
                runtime_context.update(skill=selected_skill, task=None)
                trace_event(
                    "runtime.skill.activated", "intent-router",
                    {"skill": selected_skill, "task": selected_task},
                    [route_event["eventId"]],
                )
                runtime_changed = True
            if selected_task and selected_task in runtime.tasks:
                runtime.select_task(selected_task)
                runtime_context["task"] = selected_task
                matched_task = selected_task
                runtime_changed = True
                trace_event(
                    "runtime.task.selected", "intent-router", {"task": selected_task},
                    [route_event["eventId"]],
                )
        elif runtime.task and explicit_switch:
            matched_task = runtime.match_task(transcript)
            if matched_task:
                runtime_context["task"] = matched_task
                runtime_changed = True
                trace_event("runtime.task.selected", "spoken-intent", {"task": matched_task}, [route_event["eventId"]])

        episodic.remember(
            transcript,
            kind="utterance",
            source="input-audio-transcript",
            skill=runtime_context["skill"],
            task=runtime_context["task"],
            evidence_event_id=transcript_event["eventId"],
        )
        episodic.retrieve(transcript)
        if runtime.task:
            runtime.task.voice(transcript)
        if runtime.task and any(word in transcript for word in ("记住", "以后", "每次")) and any(
            word in transcript for word in ("提醒", "慢一点", "提前说", "别催")
        ):
            step = runtime.task.current_step
            memory.propose_preference(
                task=runtime.task.spec.id,
                step=step.id,
                prompt=transcript[:160],
                preference=transcript[:160],
                source_quote=transcript[:160],
                evidence=f"用户明确提出长期偏好：{transcript[:160]}",
                scope_reason=f"话语发生时活动范围为 task={runtime.task.spec.id}, checkpoint={step.id}",
            )
        hint, recalled = apply_memory_hint() if live_bridges else (find_memory_hint(), False)
        decision = runtime.task.decision if runtime.task else {
            "action": "ask" if runtime.skill else "guide",
            "message": "请确认具体目标" if runtime.skill else "通用对话",
            "reason": "task-unresolved" if runtime.skill else "no-skill-required",
            "confidence": 1.0 if runtime.skill else 0.0,
        }
        return {
            "matchedTask": matched_task,
            "runtimeChanged": runtime_changed,
            "decision": decision,
            "memoryHint": hint,
            "memoryRecalled": recalled,
        }

    async def post_voice(request):
        result = process_transcript(str((await request.json()).get("transcript", "")))
        refresh_live_context()
        if result["memoryRecalled"]:
            request_memory_speech(result["memoryHint"])
        return web.json_response(state())

    async def post_observation(request):
        if runtime.task is None:
            raise ValueError("no active task for structured observation")
        observation = await request.json()
        runtime.task.ingest(observation)
        episodic.remember(
            observation.get("summary", ""), kind="observation",
            source=str(observation.get("source", "external-observation")),
            skill=runtime_context["skill"], task=runtime_context["task"],
        )
        hint, recalled = apply_memory_hint() if live_bridges else (find_memory_hint(), False)
        if recalled:
            request_memory_speech(hint)
        else:
            request_decision_speech(runtime.task.decision)
        return web.json_response(state())

    async def post_replay_observation(request):
        if runtime.task is None:
            raise ValueError("no active task for replay observation")
        observation = runtime.task.replay_observation(str((await request.json()).get("kind", "progress")))
        runtime.task.ingest(observation)
        episodic.remember(
            observation.get("summary", ""), kind="observation",
            source=str(observation.get("source", "research-replay")),
            skill=runtime_context["skill"], task=runtime_context["task"],
        )
        hint, recalled = apply_memory_hint() if live_bridges else (find_memory_hint(), False)
        if recalled:
            request_memory_speech(hint)
        else:
            request_decision_speech(runtime.task.decision)
        return web.json_response(state())

    async def post_candidate(request):
        accepted = bool((await request.json()).get("accepted"))
        memory.resolve_candidate(accepted)
        hint, recalled = apply_memory_hint() if accepted and live_bridges else (find_memory_hint(), False)
        refresh_live_context()
        if recalled:
            request_memory_speech(hint)
        return web.json_response(state())

    async def post_delete(request):
        memory.delete_memory(str((await request.json()).get("id", "")))
        refresh_live_context()
        return web.json_response(state())

    async def post_reset(request):
        nonlocal routing_state
        await invalidate_live_sessions()
        previous = {"skill": runtime_context["skill"], "task": runtime_context["task"]}
        runtime.deactivate()
        routing_state = registry.route("")
        runtime_context.update(skill=None, task=None)
        trace_event("runtime.session.reset", "control-api", previous)
        return web.json_response(state())

    async def post_tool(request):
        if runtime.skill is None:
            raise ValueError("no active skill exposes tools")
        payload = await request.json()
        result = tools.execute(
            tool=str(payload.get("tool", "")),
            arguments=payload.get("arguments", {}),
            declared_tools=runtime.skill.manifest.get("tools", []),
            confirmed=bool(payload.get("confirmed", False)),
        )
        return web.json_response({"result": result, "state": state()})

    async def realtime(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=3_000_000)
        await ws.prepare(request)
        api_key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            await ws.send_json({"type": "error", "message": "QWEN_API_KEY is not configured"})
            await ws.close()
            return ws

        async def emit(payload):
            if not ws.closed:
                await ws.send_json(payload)

        bridge = None

        def transcript_received(text):
            result = process_transcript(text)
            if bridge:
                bridge.update_context(context())
                if result["runtimeChanged"]:
                    request_bridge_response(
                        bridge,
                        control_instructions(text),
                        priority=100,
                        interrupt=True,
                        speech_policy="immediate",
                        reason="intent-routed-context-update",
                    )
            if result["memoryRecalled"]:
                request_memory_speech(result["memoryHint"])
            if not ws.closed:
                async def push_state():
                    await ws.send_json({"type": "state", "state": state()})
                asyncio.create_task(push_state())

        try:
            bridge = OmniBrowserBridge(api_key, model, voice, context(), emit, transcript_received)
            if hasattr(bridge, "set_trace_sink"):
                bridge.set_trace_sink(trace_event)
            live_bridges.add(bridge)
            live_sockets.add(ws)
            await bridge.connect()
            initial_hint, initial_recall = apply_memory_hint()
            if initial_recall:
                request_memory_speech(initial_hint)
            await ws.send_json({"type": "state", "state": state()})
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                kind = payload.get("type")
                if kind == "audio":
                    audio_value = str(payload.get("audio", ""))
                    video_flushed = bridge.append_audio(audio_value)
                    trace_event("media.audio.appended", "browser-microphone", {"base64Bytes": len(audio_value), "videoFlushed": video_flushed})
                    if video_flushed:
                        interval = runtime.skill.manifest.get("proactive_check_seconds", 4) if runtime.skill else 4
                        bridge.maybe_check_proactively(float(interval))
                elif kind == "video":
                    image_value = str(payload.get("image", ""))
                    trace_event("media.video.latest-buffered", "browser-camera", {"base64Bytes": len(image_value), "policy": "latest-only"})
                    if bridge.append_video(image_value):
                        interval = runtime.skill.manifest.get("proactive_check_seconds", 4) if runtime.skill else 4
                        bridge.maybe_check_proactively(float(interval))
                elif kind == "media_config":
                    media_configuration.clear()
                    media_configuration.update(payload.get("audio", {}))
                    trace_event(
                        "media.audio.configuration", "browser-media",
                        {"audio": dict(media_configuration), "playbackCapture": bool(payload.get("playbackCapture", False))},
                    )
                elif kind == "cancel":
                    bridge.cancel_response()
                elif kind == "control":
                    transcript = str(payload.get("transcript", "")).strip()
                    if not transcript:
                        continue
                    result = process_transcript(transcript)
                    bridge.update_context(context())
                    await ws.send_json({
                        "type": "user_transcript", "speaker": "user",
                        "channel": "explicit_control", "echoRisk": "low", "text": transcript,
                    })
                    await ws.send_json({"type": "state", "state": state()})
                    await ws.send_json({"type": "interrupted"})
                    instructions = control_instructions(transcript)
                    if result["memoryRecalled"]:
                        instructions += f" 同时自然融入这条用户确认记忆：{result['memoryHint']['prompt']}"
                    request_bridge_response(
                        bridge, instructions, priority=100, interrupt=True,
                        speech_policy="immediate", reason="explicit-user-control",
                    )
                elif kind in {"state", "refresh"}:
                    bridge.update_context(context())
                    await ws.send_json({"type": "state", "state": state()})
        except Exception as exc:
            if not ws.closed:
                await ws.send_json({"type": "error", "message": str(exc)})
        finally:
            if bridge:
                live_bridges.discard(bridge)
                await bridge.close()
            live_sockets.discard(ws)
        return ws

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            request_id = uuid4().hex[:10]
            LOGGER.exception("request %s failed: %s %s", request_id, request.method, request.path)
            return web.json_response(
                {"error": "internal_error", "message": "请求处理失败，请查看服务端日志。", "requestId": request_id},
                status=500,
            )

    app = web.Application(client_max_size=3_000_000, middlewares=[errors])
    app.add_routes([
        web.get("/api/state", get_state),
        web.post("/api/skills/activate", post_activate),
        web.post("/api/tasks/select", post_task_select),
        web.post("/api/tasks/create", post_task_create),
        web.post("/api/voice", post_voice),
        web.post("/api/task/observe", post_observation),
        web.post("/api/replay/observe", post_replay_observation),
        web.post("/api/task/reset", post_reset),
        web.post("/api/candidate", post_candidate),
        web.post("/api/memory/delete", post_delete),
        web.post("/api/tools/execute", post_tool),
        web.get("/ws/realtime", realtime),
        web.get("/", get_index),
        web.static("/", WEB_ROOT),
    ])
    return app


def main():
    local_root = runtime_root()
    parser = argparse.ArgumentParser(description="EgoAssist full-duplex embodied assistance framework")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--skills", default=str(BUILTIN_SKILLS_ROOT))
    parser.add_argument("--active-skill", default=None, help="Optional startup Skill; default is generic unloaded mode")
    parser.add_argument("--memory-path", default=str(local_root / ".egoassist" / "preferences.json"))
    parser.add_argument("--model", default="qwen3.5-omni-flash-realtime")
    parser.add_argument("--voice", default="Ethan")
    args = parser.parse_args()
    log_root = local_root / ".egoassist"
    log_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_root / "server.log",
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env_path = local_root / ".env"
    load_env(str(env_path) if env_path.is_file() else None)
    from aiohttp import web

    if args.open:
        import threading
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
    web.run_app(
        create_app(args.skills, args.active_skill, args.memory_path, args.model, args.voice),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
