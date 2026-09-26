from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from egoassist import CONSOLE_ROOT, PACKAGE_ROOT
from egoassist.application.evidence_api import EvidenceEndpoints
from egoassist.application.identity_api import IdentityEndpoints
from egoassist.application.memory_api import MemoryEndpoints
from egoassist.application.realtime_connection import RealtimeConnection
from egoassist.application.retention import AccountRetention
from egoassist.application.state import StateProjection
from egoassist.application.task_api import TaskEndpoints
from egoassist.core.settings import number, tool_calls_enabled
from egoassist.memory import ClockContext, TimelineMemory
from egoassist.memory.commit import MemoryCommitService
from egoassist.memory.service import MemoryService
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.memory.insights import InsightGenerator
from egoassist.memory.recovery import MemoryRecovery
from egoassist.product import (
    LocalArtifactAdapter,
    OutcomeLedger,
    PerceptionFrameContext,
    PerceptionHypothesis,
    PrivacyGovernance,
    ProductDatabase,
    WebhookHumanHelpAdapter,
)
from egoassist.product.answer_jobs import AnswerJobs
from egoassist.product.insight_jobs import InsightJobs
from egoassist.product.notifications import NtfyNotificationAdapter
from egoassist.product.reminders import (
    DEFAULT_TASK_QUEUE,
    ReminderActivities,
    ReminderWorkflow,
    ReminderWorkflowInput,
    TemporalReminderCoordinator,
)
from egoassist.prompts import prompt_text
from egoassist.realtime import (
    AmbientWatchList,
    AmbientWatchSession,
    CaseWorkLedger,
    ContextHarness,
    ContextLayer,
    ContextSettings,
    EventLedger,
    IdentityStore,
    LiveKitInteractionTransport,
    LiveKitSettings,
    RealtimeEpisodeMemory,
    SessionJournal,
    SkillRegistry,
    SkillRuntime,
    SkillToolExecutor,
    TaskSpec,
    TurnTakingPolicy,
    default_memory_policy,
    livekit_capability,
    platform_capability_available,
    public_tool_declaration,
    validate_perception_result,
)
from egoassist.realtime.interaction_service import (
    DefaultInteractionState,
    InteractionService,
)
from egoassist.realtime.locale_prompts import PROMPTS
from egoassist.realtime.tools import tool_result_succeeded
from egoassist.timefmt import format_beijing_iso

WEB_ROOT = CONSOLE_ROOT

LOGGER = logging.getLogger("egoassist.realtime")

AUTH_COOKIE = "egoassist_session"

TASK_CONTROL_LABELS = {
    None,
    "confirm-step",
    "rollback",
    "repeat",
    "pause-task",
    "resume-task",
    "request-help",
    "show-reference",
}

SKILL_CONTROL_LABELS = {None, "deactivate"}

AMBIENT_CONSENT_PURPOSE = "ambient-observation"


from egoassist.application.ownership import (
    CaseOwnerCapacityError,
    CaseRuntimeOwner,
    MemoryEventBus,
    RuntimeRootLease,
    TaskSlot,
)

try:
    from temporalio.worker import Worker
except ImportError:
    Worker = None


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean environment flag. Anything unrecognized keeps the default."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def create_app(
    skills_path: str, active_skill: str | None, memory_path: str, model: str, voice: str
):
    from aiohttp import WSMsgType, web

    from egoassist.realtime.omni import OmniBrowserBridge

    backend = os.getenv("EGOASSIST_REALTIME_BACKEND", "omni").strip().lower()
    if backend not in {"omni", "modular"}:
        raise ValueError("EGOASSIST_REALTIME_BACKEND must be omni or modular")
    if backend == "modular":
        from egoassist.realtime.modular import settings as modular_settings
        model = modular_settings()["textModel"]

    user_key = web.RequestKey("egoassist.user", object)

    registry = SkillRegistry(skills_path)
    ledger = EventLedger()
    runtime_context = {"skill": active_skill or None, "task": None}
    episodic = None

    def trace_event(event_type, source, payload, parent_event_ids=None, **metadata):
        event = ledger.append(
            event_type,
            source,
            payload,
            parent_event_ids=parent_event_ids or [],
            model=metadata.get("model", model),
            skill=runtime_context["skill"],
            task=runtime_context["task"],
            response_id=metadata.get("response_id"),
            latency_ms=metadata.get("latency_ms"),
        )
        if episodic and event_type == "proactive.gate.decision":
            memory_service.remember_scene(episodic, payload, policy=default_memory_policy(),
                source_id=event['eventId'], skill=runtime_context['skill'], task=runtime_context['task'])
        return event

    runtime = SkillRuntime(registry, active_skill, event_sink=trace_event)
    if runtime.active:
        runtime_context.update(
            skill=runtime.skill.name,
            task=runtime.task.spec.id if runtime.task else None,
        )
    timeline_path = Path(memory_path).with_name("timeline.sqlite3")
    timeline = TimelineMemory(timeline_path, event_sink=trace_event)
    episodic = RealtimeEpisodeMemory(
        timeline_path,
        event_sink=trace_event,
        timeline=timeline,
        owner_user_id="local-default",
        scope="participant",
    )
    identity = IdentityStore(Path(memory_path).with_name("identity.sqlite3"))
    product_database = ProductDatabase(Path(memory_path).with_name("product.sqlite3"))
    product_database.create_schema()
    # Journal projection is idempotent but still performs many SQLite reads.
    # Cache the result by journal length/mtime so polling /api/state does not
    # replay the entire session on every request.
    projection_cache: dict[str, tuple[int, int, dict[str, int]]] = {}
    capability_overrides: dict[str, dict[str, Any]] = {}
    artifact_catalog = [
        {**dict(item), "root": package.root, "skill": package.name}
        for package in registry.skills.values()
        for item in package.manifest.get("artifacts", [])
    ]
    artifact_port = LocalArtifactAdapter(artifact_catalog)
    human_help_port = WebhookHumanHelpAdapter.from_env()
    capability_overrides["reference-display"] = {
        "status": "available" if artifact_catalog else "unconfigured",
        "reason": (
            "Task-scoped artifact authorization and the PDF.js/image viewer are connected."
            if artifact_catalog
            else "No Skill declares a reference artifact."
        ),
    }
    capability_overrides["human-help"] = {
        "status": "available" if human_help_port is not None else "unconfigured",
        "reason": (
            "A consent-gated webhook connector with durable receipts is configured."
            if human_help_port is not None
            else "EGOASSIST_HUMAN_HELP_WEBHOOK is not configured."
        ),
    }
    livekit_settings = (
        LiveKitSettings.from_env() if LiveKitSettings is not None else None
    )
    livekit_transport = (
        LiveKitInteractionTransport(livekit_settings)
        if livekit_settings is not None
        else None
    )
    capability_overrides["realtime-media"] = livekit_capability(livekit_settings)
    reminder_runtime: dict[str, Any] = {
        "status": "unconfigured",
        "coordinator": None,
        "worker": None,
        "workerTask": None,
        "reason": "EGOASSIST_TEMPORAL_TARGET is not configured.",
    }
    reminder_start_tasks: set[asyncio.Task] = set()

    # --- Ambient (task-free) observation -----------------------------------
    # Enabled by default, but no frame reaches the provider without live account
    # consent and an applicable watchlist (ADR 007).
    ambient_settings: dict[str, Any] = {
        "enabled": _env_flag("EGOASSIST_AMBIENT_MODE", True),
        "watchlist_path": os.getenv("EGOASSIST_AMBIENT_WATCHLIST", "").strip(),
        "interval_seconds": max(
            2.0, float(os.getenv("EGOASSIST_AMBIENT_INTERVAL_SECONDS", "5") or 5)
        ),
        "watchlist": None,
        "reason": "EGOASSIST_AMBIENT_MODE is off.",
    }
    if ambient_settings["enabled"]:
        candidate = ambient_settings["watchlist_path"] or str(
            PACKAGE_ROOT / "watchlists" / "open.json"
        )
        try:
            ambient_settings["watchlist"] = AmbientWatchList.load(candidate)
            ambient_settings["reason"] = f"Watching {Path(candidate).name}."
        except (OSError, ValueError) as exc:
            # Fail closed: a bad watchlist disables ambient rather than falling
            # back to some default set of things to watch.
            ambient_settings["enabled"] = False
            ambient_settings["reason"] = (
                f"Watchlist unusable ({str(exc)[:160]}); ambient disabled."
            )
    capability_overrides["ambient-observation"] = {
        "status": "available" if ambient_settings["enabled"] else "unconfigured",
        "reason": ambient_settings["reason"],
    }

    def ambient_consent(user_id: str) -> dict[str, Any] | None:
        """The active ambient grant for one account, or None."""
        return product_database.active_consent(user_id, purpose=AMBIENT_CONSENT_PURPOSE)

    ambient_control_revisions: dict[str, int] = {}
    ambient_control_epoch = uuid4().hex
    ambient_control_locks: dict[str, asyncio.Lock] = {}

    def ambient_options(owner):
        if owner.ambient_options is None:
            saved = next(
                (
                    e["settings"]
                    for e in reversed(owner.journal.entries(types={"ambient.settings"}, tail=1))
                    if e.get("type") == "ambient.settings"
                ),
                {},
            )
            owner.ambient_options = {
                "intervalSeconds": ambient_settings["interval_seconds"],
                "questionsEnabled": False,
                "remindersEnabled": True,
                "reminderGoal": "",
                "questionCooldownSeconds": number(
                    "EGOASSIST_AMBIENT_QUESTION_COOLDOWN", 90, maximum=3600
                ),
                **saved,
            }
        return owner.ambient_options

    def ambient_watch_session(owner: "CaseRuntimeOwner") -> Any:
        """Lazily create the watch session, but only while consent is live.

        Re-checked on every frame rather than cached at session start, so a
        revocation stops observation without needing a reconnect.
        """
        if not ambient_settings["enabled"] or ambient_settings["watchlist"] is None:
            return None
        grant = ambient_consent(owner.user_id)
        if grant is None:
            owner.ambient = None
            return None
        existing = owner.ambient
        if existing is not None and existing.consent_id == grant["consent_id"]:
            return existing
        owner.ambient = AmbientWatchSession(
            ambient_settings["watchlist"],
            consent_id=grant["consent_id"],
        )
        previous = next(
            (
                e
                for e in reversed(owner.journal.entries(types={"ambient_observation"}))
                if e.get("type") == "ambient_observation"
                and e.get("status") == "accepted"
            ),
            None,
        )
        if previous:
            owner.ambient.ingest(previous.get("evidence", {}))
        return owner.ambient

    def runtime_tool_declarations(package) -> list[dict]:
        declarations = []
        timer_available = platform_capability_available(
            "timer-delivery",
            capability_overrides,
        )
        for raw in package.manifest.get("tools", []):
            item = dict(raw)
            if item.get("name") == "set_timer" and timer_available:
                item["availability"] = {
                    "status": "available",
                    "reason": "Temporal workflow and durable ResultInbox delivery are connected.",
                }
                item["handler"] = "builtin.temporal-reminder.v1"
            if item.get("name") == "show_reference" and any(
                str(artifact.get("skill")) == package.name
                for artifact in artifact_catalog
            ):
                item["availability"] = {
                    "status": "available",
                    "reason": "A task-scoped artifact resolver and browser viewer are connected.",
                }
                item["handler"] = "builtin.artifact-reference.v1"
            if not tool_calls_enabled():
                item["availability"] = {"status": "unconfigured", "reason": "工具调用已关闭。"}
            declarations.append(item)
        return declarations

    def artifacts_for_runtime(
        active_runtime: SkillRuntime, owner_id: str
    ) -> list[dict[str, Any]]:
        if active_runtime.skill is None or active_runtime.task is None:
            return []
        return artifact_port.list_for_task(
            owner_id=owner_id,
            skill=active_runtime.skill.name,
            task_id=active_runtime.task.spec.id,
            step_id=active_runtime.task.current_step.id,
        )

    def perception_context_for_runtime(
        active_runtime: SkillRuntime,
        *,
        user_id: str,
        session_id: str,
        frame_id: str,
        captured_at: datetime,
    ) -> PerceptionFrameContext | None:
        if active_runtime.skill is None or active_runtime.task is None:
            return None
        binding = active_runtime.task.perception_binding()
        return PerceptionFrameContext(
            context_id=binding["contextId"],
            frame_id=frame_id,
            user_id=user_id,
            session_id=session_id,
            skill_id=active_runtime.skill.name,
            task_id=binding["taskId"],
            task_version=binding["taskVersion"],
            task_provenance=binding["taskProvenance"],
            checkpoint_id=binding["checkpointId"],
            checkpoint_epoch=binding["checkpointEpoch"],
            allowed_transitions=binding["allowedTransitions"],
            completion_criteria=binding["completionCriteria"],
            risk=binding["risk"],
            source_reference=binding["sourceReference"],
            authorized_artifacts=artifacts_for_runtime(active_runtime, user_id),
            frame_captured_at=captured_at,
        )

    def runtime_skill_summary(package) -> dict:
        summary = package.public_summary()
        summary["tools"] = [
            public_tool_declaration(item) for item in runtime_tool_declarations(package)
        ]
        return summary

    def runtime_skill_summaries() -> list[dict]:
        return [runtime_skill_summary(package) for package in registry.skills.values()]

    user_memory_root = Path(memory_path).with_name("users")
    from egoassist.memory.evidence_service import EvidenceService
    evidence_service = EvidenceService(user_memory_root)
    insight_generator = InsightGenerator(
        os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or ""
    )
    deleting_insight_users: set[str] = set()
    active_account_requests = {}

    def insight_sources(user_id, session_id, scope):
        identity.get_context_session(user_id, session_id)
        return (
            [case_owner(user_id, session_id).journal.path]
            if scope == "case"
            else sorted((user_memory_root / user_id / "sessions").glob("*.jsonl"))
        )

    insight_executor = InsightJobs(insight_generator, insight_sources)
    answer_executor = AnswerJobs(insight_generator.api_key)
    memory_recovery = MemoryRecovery(
        SemanticTurnRouter(insight_generator.api_key, timeout_seconds=number("EGOASSIST_MEMORY_RECOVERY_TIMEOUT", 20)),
        timeline,
        lambda user, session: identity.get_context_session(user, session)[
            "memoryPolicy"
        ],
    )
    user_episodics: dict[str, RealtimeEpisodeMemory] = {}
    case_episodics: dict[tuple[str, str], RealtimeEpisodeMemory] = {}
    user_runtimes: dict[str, SkillRuntime] = {}
    user_routing: dict[str, dict] = {}
    user_tools: dict[str, SkillToolExecutor] = {}

    def clock_for_session(user_id: str, session_id: str) -> ClockContext:
        experiment_case = identity.get_context_session(user_id, session_id)
        return ClockContext.current(
            experiment_case.get("timezone", "UTC"),
            experiment_case.get("utcOffsetMinutes", 0),
        )

    def episodic_for_user(user_id: str) -> RealtimeEpisodeMemory:
        if user_id not in user_episodics:
            user_episodics[user_id] = RealtimeEpisodeMemory(
                timeline_path,
                event_sink=trace_event,
                timeline=timeline,
                owner_user_id=user_id,
                scope="participant",
            )
        return user_episodics[user_id]

    def episodic_for_case(user_id: str, session_id: str) -> RealtimeEpisodeMemory:
        key = (user_id, session_id)
        if key not in case_episodics:
            case_episodics[key] = RealtimeEpisodeMemory(
                timeline_path,
                event_sink=trace_event,
                timeline=timeline,
                owner_user_id=user_id,
                owner_case_id=session_id,
                scope="case",
            )
        return case_episodics[key]

    def scoped_episodic_memory(
        user_id: str,
        session_id: str | None,
        policy: dict,
    ) -> RealtimeEpisodeMemory:
        scope = policy["components"]["timeline"]["scope"]
        if scope == "case" and session_id:
            return episodic_for_case(user_id, session_id)
        return episodic_for_user(user_id)

    def migrate_legacy_memories() -> None:
        if not user_memory_root.exists():
            return
        for user_dir in user_memory_root.iterdir():
            if not user_dir.is_dir():
                continue
            user_id = user_dir.name
            episodic_for_user(user_id).import_preference_legacy(
                user_dir / "preferences.json"
            )
            episodic_for_user(user_id).import_legacy(user_dir / "episodes.json")
            cases_root = user_dir / "cases"
            if not cases_root.exists():
                continue
            for case_dir in cases_root.iterdir():
                if not case_dir.is_dir():
                    continue
                session_id = case_dir.name
                episodic_for_case(user_id, session_id).import_preference_legacy(
                    case_dir / "preferences.json"
                )
                episodic_for_case(user_id, session_id).import_legacy(
                    case_dir / "episodes.json"
                )

    migrate_legacy_memories()
    outcome_ledger = OutcomeLedger(Path(memory_path).with_name("outcomes.jsonl"))
    privacy_governance = PrivacyGovernance(
        identity=identity,
        product_database=product_database,
        timeline=timeline,
        user_memory_root=user_memory_root,
        outcome_ledger=outcome_ledger,
    )

    def runtime_for_user(user_id: str) -> SkillRuntime:
        if user_id not in user_runtimes:
            user_runtimes[user_id] = SkillRuntime(registry, active_skill)
            user_routing[user_id] = registry.route("")
            user_tools[user_id] = SkillToolExecutor()
        return user_runtimes[user_id]

    tools = SkillToolExecutor()
    live_bridges: set[OmniBrowserBridge] = set()
    live_sockets: set = set()
    live_harnesses: dict[str, ContextHarness] = {}
    live_sockets_by_user: dict[str, set] = {}
    live_sockets_by_session: dict[str, set] = {}
    case_owners: OrderedDict[str, CaseRuntimeOwner] = OrderedDict()
    max_case_owners = max(1, int(os.getenv("EGOASSIST_MAX_CASE_OWNERS", "128")))
    default_interaction = DefaultInteractionState()
    default_interaction.routing = registry.route("")
    media_configuration: dict = {}
    default_interaction.episode_injection = {
        "status": "no-evidence",
        "query": "",
        "episodeIds": [],
        "evidence": [],
    }

    def context(
        target_runtime: SkillRuntime | None = None,
        *,
        episode_injection: dict | None = None,
        include_untrusted_evidence: bool = False,
        include_skill_policy: bool = True,
    ) -> str:
        active_runtime = target_runtime or runtime
        # Durable user data is projected as typed low-trust messages by the
        # harness. It must never be interpolated into provider instructions.
        base = active_runtime.context(
            [],
            runtime_tool_declarations(active_runtime.skill)
            if active_runtime.skill
            else None,
            include_skill_policy=include_skill_policy,
        )
        evidence = (episode_injection or default_interaction.episode_injection).get(
            "evidence", []
        )
        if not include_untrusted_evidence or not evidence:
            return base
        lines = [
            PROMPTS.evidence_header,
            PROMPTS.evidence_instruction,
        ]
        for item in evidence:
            bj_time = format_beijing_iso(item.get("timestamp")) or item.get(
                "timestamp", ""
            )
            lines.append(
                f"- episode={item['episodeId']} time={bj_time} "
                f"kind={item.get('kind', '?')} source={item['source']} "
                f"score={item['score']}: {item['summary']}"
            )
        return base + "\n" + "\n".join(lines)

    def harness_layers(
        target_runtime: SkillRuntime | None = None,
    ) -> list[ContextLayer]:
        active_runtime = target_runtime or runtime
        catalog = registry.routing_catalog()
        layers = [
            ContextLayer(
                "core-policy",
                context(target_runtime, include_skill_policy=False),
                trust="system",
                priority=100,
                required=True,
                source="skill-runtime",
            ),
            ContextLayer(
                "context-trust-policy",
                prompt_text("run_egoassist/harness_layers.1"),
                trust="system",
                priority=100,
                required=True,
                source="context-harness",
            ),
        ]
        if active_runtime.skill:
            layers.append(
                ContextLayer(
                    "active-skill-policy",
                    active_runtime.skill.instruction_context(),
                    trust="system",
                    priority=95,
                    required=True,
                    source=f"skill:{active_runtime.skill.name}:{active_runtime.skill.content_hash}",
                )
            )
        else:
            layers.append(
                ContextLayer(
                    "skill-catalog",
                    "<available_skills>\n"
                    + json.dumps(catalog, ensure_ascii=False, sort_keys=True)
                    + prompt_text("run_egoassist/harness_layers.2"),
                    trust="system",
                    priority=90,
                    required=True,
                    source="skill-registry",
                )
            )
        live = active_runtime.live_state_text()
        if live:
            layers.append(
                ContextLayer(
                    "skill-runtime-state",
                    live,
                    trust="system",
                    priority=85,
                    required=True,
                    source="skill-runtime",
                )
            )
        return layers

    # After this many uninterrupted DashScope exchanges in one session, force a
    # compact+reconnect so DashScope's live context window is reset to the summary
    # + recent tail. Prevents first_text_delay from growing into the 100ms range.
    # Override with EGOASSIST_COMPACT_AFTER_EXCHANGES (0 = disabled).
    _COMPACT_AFTER_EXCHANGES = int(os.getenv("EGOASSIST_COMPACT_AFTER_EXCHANGES", "25"))

    def refresh_harness_context(
        bridge,
        harness: ContextHarness,
        target_runtime: SkillRuntime | None = None,
    ) -> dict:
        exchanges = getattr(bridge, "_dialogue_exchanges", 0)
        if _COMPACT_AFTER_EXCHANGES > 0 and exchanges >= _COMPACT_AFTER_EXCHANGES:
            harness.compact(force=True, reason="dialogue-exchange-threshold")
        built = harness.build(harness_layers(target_runtime))
        if hasattr(bridge, "apply_context_projection"):
            bridge.apply_context_projection(built["system"], built["manifest"])
        else:
            bridge.update_context(built["system"])
        return built

    def runtime_checkpoint(target_runtime: SkillRuntime | None = None) -> dict:
        active_runtime = target_runtime or runtime
        return {
            "skill": active_runtime.skill.name if active_runtime.skill else None,
            "skillContentHash": active_runtime.skill.content_hash
            if active_runtime.skill
            else None,
            "task": active_runtime.task.spec.id if active_runtime.task else None,
            "taskSpec": active_runtime.task.spec.to_dict()
            if active_runtime.task
            else None,
            "taskState": active_runtime.task.snapshot()
            if active_runtime.task
            else None,
        }

    def session_runtime_from(
        journal: SessionJournal,
        seed_runtime: SkillRuntime | None = None,
        checkpoint_override: dict | None = None,
    ) -> SkillRuntime:
        def session_event(
            event_type, source, payload, parent_event_ids=None, **metadata
        ):
            entry = journal.append(
                "runtime_event",
                {
                    "eventType": event_type,
                    "source": source,
                    "payload": dict(payload or {}),
                    "parentEventIds": list(parent_event_ids or []),
                    "metadata": dict(metadata),
                },
            )
            return {**entry, "eventId": entry["id"]}

        checkpoint = checkpoint_override or next(
            (
                entry.get("state")
                for entry in reversed(journal.entries(types={"state_checkpoint"}, tail=1))
                if entry.get("type") == "state_checkpoint"
            ),
            None,
        )
        durable = (
            checkpoint
            if isinstance(checkpoint, dict)
            else runtime_checkpoint(seed_runtime or runtime)
        )
        restored = SkillRuntime(registry, None)

        def finished() -> SkillRuntime:
            restored.set_event_sink(session_event)
            return restored

        skill_name = durable.get("skill")
        if not skill_name:
            return finished()
        restored.activate(str(skill_name), select_default=False)
        task_spec_payload = durable.get("taskSpec")
        task_state = durable.get("taskState")
        if not isinstance(task_spec_payload, dict) or not isinstance(task_state, dict):
            return finished()
        spec = TaskSpec.from_dict(
            task_spec_payload,
            provenance=str(task_spec_payload.get("provenance", "session")),
        )
        if (
            spec.id in restored.tasks
            and restored.tasks[spec.id].version == spec.version
        ):
            restored.select_task(spec.id)
        else:
            restored.create_task(spec)
        restored.task.restore(task_state)
        return finished()

    def append_memory_audit(
        target: ContextHarness | SessionJournal,
        event_type: str,
        source: str,
        payload: dict,
        *,
        parent_event_id: str | None = None,
    ) -> dict:
        journal = target.journal if isinstance(target, ContextHarness) else target
        return journal.append(
            "runtime_event",
            {
                "eventType": event_type,
                "source": source,
                "payload": dict(payload),
                "model": model,
                "skill": None,
                "task": None,
                "responseId": None,
                "latencyMs": None,
            },
            parent_id=parent_event_id,
        )

    def case_journal(
        user_id: str,
        session_id: str,
        *,
        initial_memory_policy: dict | None = None,
    ) -> SessionJournal:
        return case_owner(
            user_id,
            session_id,
            initial_memory_policy=initial_memory_policy,
        ).journal

    def case_owner(
        user_id: str,
        session_id: str,
        *,
        initial_memory_policy: dict | None = None,
    ) -> CaseRuntimeOwner:
        existing = case_owners.get(session_id)
        if existing is not None:
            if existing.user_id != user_id:
                raise PermissionError("context session belongs to another user")
            case_owners.move_to_end(session_id)
            if existing.scene:
                existing.scene.recover()
            return existing
        if len(case_owners) >= max_case_owners:
            evicted = False
            for candidate_id, candidate in tuple(case_owners.items()):
                worker_active = (
                    candidate.turn_worker is not None
                    and not candidate.turn_worker.done()
                )
                maintenance_active = (
                    candidate.maintenance_task is not None
                    and not candidate.maintenance_task.done()
                )
                scheduler_active = (
                    candidate.job_scheduler_task is not None
                    and not candidate.job_scheduler_task.done()
                )
                if (
                    candidate.socket_token is None
                    and candidate.socket is None
                    and not worker_active
                    and not maintenance_active
                    and not scheduler_active
                    and not (candidate.scene and candidate.scene.tasks)
                    and not candidate.transitioning
                    and not candidate.mutation_lock.locked()
                ):
                    case_owners.pop(candidate_id, None)
                    case_episodics.pop((candidate.user_id, candidate_id), None)
                    evicted = True
                    break
            if not evicted:
                raise CaseOwnerCapacityError(
                    f"all {max_case_owners} cached case runtimes are active; retry after a case disconnects"
                )
        experiment_case = identity.get_context_session(user_id, session_id)
        journal = SessionJournal.open_in(
            user_memory_root / user_id / "sessions",
            session_id,
            metadata={
                "model": model,
                "voice": voice,
                "userId": user_id,
                "caseLabel": experiment_case.get("label", ""),
                "initialMemoryPolicy": initial_memory_policy
                or experiment_case["memoryPolicy"],
            },
        )
        seed = SkillRuntime(registry, active_skill)
        active_runtime = session_runtime_from(journal, seed)
        audit = [
            dict(entry["audit"])
            for entry in journal.entries(types={"tool_audit"})
            if entry.get("type") == "tool_audit"
            and isinstance(entry.get("audit"), dict)
        ]
        owner = CaseRuntimeOwner(
            user_id=user_id,
            session_id=session_id,
            journal=journal,
            harness=ContextHarness(
                journal,
                ContextSettings(
                    context_window=int(os.getenv("EGOASSIST_CONTEXT_WINDOW", "32768")),
                    reserve_tokens=int(os.getenv("EGOASSIST_CONTEXT_RESERVE", "4096")),
                    keep_recent_tokens=int(
                        os.getenv("EGOASSIST_CONTEXT_KEEP_RECENT", "6000")
                    ),
                    max_summary_tokens=int(
                        os.getenv("EGOASSIST_CONTEXT_SUMMARY", "3000")
                    ),
                ),
            ),
            runtime=active_runtime,
            routing=routing_for_case(journal),
            tools=SkillToolExecutor(audit=audit),
            work_ledger=CaseWorkLedger(journal),
        )
        case_owners[session_id] = owner
        from egoassist.realtime.interaction import InteractionState

        owner.interaction = InteractionState(journal)
        from egoassist.realtime.scene import SharedScene

        owner.scene = SharedScene(
            journal, os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or ""
        )
        owner.scene.recover()

        # Phase 1: Memory observability bus
        trace_dir = Path(journal.path).parent
        owner.memory_bus = MemoryEventBus(
            session_id=session_id,
            trace_path=trace_dir / "memory_trace.jsonl",
        )
        # Hook: context compaction
        owner.harness.on_compact = lambda result: owner.memory_bus.record(
            "compact",
            tokens_before=result.get("tokensBefore"),
            tokens_after=result.get("retainedTailTokens"),
            reason=result.get("reason"),
            algorithm=result.get("algorithm"),
            compacted_count=len(result.get("compactedEntryIds", [])),
        )

        recover_case_reminders(owner)
        ensure_job_scheduler(owner)
        return owner

    def runtime_for_case(
        user_id: str, session_id: str
    ) -> tuple[SessionJournal, SkillRuntime]:
        owner = case_owner(user_id, session_id)
        return owner.journal, owner.runtime

    def routing_for_case(journal: SessionJournal) -> dict:
        return next(
            (
                dict(entry["routing"])
                for entry in reversed(journal.entries(types={"routing_decision"}))
                if entry.get("type") == "routing_decision"
                and isinstance(entry.get("routing"), dict)
            ),
            registry.route(""),
        )

    def persist_tool_audit(owner: CaseRuntimeOwner, previous_count: int) -> None:
        for item in owner.tools.audit[previous_count:]:
            owner.journal.append("tool_audit", {"audit": dict(item)})

    def request_timeline_consolidation(owner: CaseRuntimeOwner) -> None:
        """Coalesce maintenance requests into one tracked per-case task."""
        owner.consolidation_requested = True
        active = owner.maintenance_task
        if active is not None and not active.done():
            return

        async def consolidate_requested_scopes() -> None:
            while owner.consolidation_requested:
                owner.consolidation_requested = False

                results = await asyncio.to_thread(memory_service.consolidate, owner.user_id, owner.session_id)
                for scope, result in zip(("case", "participant"), results):
                    if result.get("status") == "consolidated":
                        append_memory_audit(
                            owner.journal,
                            "timeline.memory.consolidated",
                            "timeline-memory",
                            {**result, "storageScope": scope},
                        )

        task = asyncio.create_task(
            consolidate_requested_scopes(),
            name=f"egoassist-memory-consolidation:{owner.session_id}",
        )
        owner.maintenance_task = task

        def maintenance_done(completed: asyncio.Task) -> None:
            if owner.maintenance_task is completed:
                owner.maintenance_task = None
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                LOGGER.exception(
                    "timeline consolidation failed for case %s",
                    owner.session_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(maintenance_done)

    def remember_tool_effect(
        owner: CaseRuntimeOwner,
        *,
        call_id: str,
        name: str,
        result: Any,
        is_error: bool,
        source_id: str | None = None,
    ) -> dict | None:
        if not tool_result_succeeded(result, is_error=is_error):
            return None
        card = memory_service.remember_tool_effect(user_id=owner.user_id, session_id=owner.session_id,
            call_id=call_id, name=name, result=result, source_id=source_id)
        if card:
            append_memory_audit(
                owner.journal,
                "timeline.memory.tool-effect.written",
                "timeline-memory",
                {
                    "cardId": card["id"],
                    "callId": call_id,
                    "tool": name,
                    "status": card["status"],
                    "sourceToolResultId": source_id,
                },
                parent_event_id=source_id,
            )
        return card

    def append_case_runtime_checkpoint(
        journal: SessionJournal,
        active_runtime: SkillRuntime,
        *,
        event_type: str,
        source: str,
        payload: dict,
        reason: str,
    ) -> None:
        journal.append(
            "runtime_event",
            {
                "eventType": event_type,
                "source": source,
                "payload": dict(payload),
                "model": model,
                "skill": active_runtime.skill.name if active_runtime.skill else None,
                "task": active_runtime.task.spec.id if active_runtime.task else None,
                "responseId": None,
                "latencyMs": None,
            },
        )
        journal.append_state_checkpoint(
            runtime_checkpoint(active_runtime), reason=reason
        )

    def transition_case_skill(
        journal: SessionJournal,
        active_runtime: SkillRuntime,
        skill_name: str | None,
        *,
        source: str,
    ) -> dict:
        before = runtime_checkpoint(active_runtime)
        previous_skill = before["skill"]
        target_skill = str(skill_name or "").strip() or None
        if target_skill:
            registry.get(target_skill)
        CaseWorkLedger(journal).cancel_pending_turns(reason=f"skill-lifecycle:{source}")
        if target_skill:
            if previous_skill == target_skill:
                action = "unchanged"
            else:
                action = "switched" if previous_skill else "activated"
                active_runtime.activate(target_skill, select_default=False)
        else:
            action = "deactivated" if previous_skill else "already-general"
            active_runtime.deactivate()
        after = runtime_checkpoint(active_runtime)
        control = journal.append(
            "control_decision",
            {
                "decision": {
                    "method": "explicit-skill-lifecycle-v1",
                    "skillControl": "deactivate" if target_skill is None else None,
                    "selectedSkill": target_skill,
                    "action": action,
                },
                "source": source,
                "input": None,
            },
        )
        routing = {
            **registry.route(""),
            "method": "explicit-skill-lifecycle-v1",
            "status": (
                "matched"
                if target_skill
                else "deactivated"
                if action == "deactivated"
                else "no-match"
            ),
            "selectedSkill": target_skill,
            "selectedTask": None,
            "transition": action,
            "claimBoundary": "Explicit typed Skill lifecycle control scoped to one experiment case.",
        }
        journal.append(
            "routing_decision",
            {
                "routing": routing,
                "source": source,
                "parentEventIds": [control["id"]],
            },
        )
        append_case_runtime_checkpoint(
            journal,
            active_runtime,
            event_type=f"runtime.skill.{action}",
            source=source,
            payload={"action": action, "before": before, "after": after},
            reason=f"skill-lifecycle:{action}:{source}",
        )
        return routing

    def memory_retrieval(
        target_runtime: SkillRuntime | None = None,
        *,
        emit: bool = False,
        episodic_memory: RealtimeEpisodeMemory | None = None,
        enabled: bool = True,
    ) -> dict:
        active_runtime = target_runtime or runtime
        active_episodic = episodic_memory or episodic
        task = active_runtime.task
        triggers = [(task.spec.id, task.current_step.id), (task.spec.title, task.current_step.title)] if task else []
        return memory_service.preference_retrieval(active_episodic, triggers, enabled=enabled, emit=emit)

    def find_memory_hint(
        memory_state: dict | None = None,
        *,
        emit: bool = False,
        target_runtime: SkillRuntime | None = None,
        episodic_memory: RealtimeEpisodeMemory | None = None,
        enabled: bool = True,
    ) -> dict | None:
        del memory_state
        selected = memory_retrieval(
            target_runtime,
            emit=emit,
            episodic_memory=episodic_memory,
            enabled=enabled,
        ).get("selected")
        return selected["record"] if selected else None

    def apply_memory_hint(
        target_runtime: SkillRuntime | None = None,
        *,
        episodic_memory: RealtimeEpisodeMemory | None = None,
        enabled: bool = True,
    ) -> tuple[dict | None, bool]:
        active_runtime = target_runtime or runtime
        if active_runtime.task is None or not enabled:
            return None, False
        hint = find_memory_hint(
            emit=True,
            target_runtime=active_runtime,
            episodic_memory=episodic_memory,
            enabled=enabled,
        )
        already_used = bool(
            hint and hint["id"] in active_runtime.task.announced_memory_ids
        )
        active_runtime.task.apply_memory_hint(hint)
        return hint, bool(hint and not already_used)

    def refresh_live_context() -> None:
        for bridge in tuple(live_bridges):
            try:
                bridge.update_context(context())
            except Exception:
                live_bridges.discard(bridge)

    def turn_policy(target_runtime: SkillRuntime | None = None) -> TurnTakingPolicy:
        active_runtime = target_runtime or runtime
        return (
            TurnTakingPolicy.from_manifest(active_runtime.skill.manifest)
            if active_runtime.skill
            else TurnTakingPolicy()
        )

    def request_bridge_response(
        bridge,
        instructions,
        *,
        priority,
        interrupt=False,
        speech_policy="immediate",
        reason="unspecified",
        max_protection_ms=2500,
        output_mode="audio",
        purpose="conversation",
        origin=None,
    ):
        playback_context = getattr(bridge, "playback_context", None)
        if playback_context:
            instructions += (
                "\n"
                + prompt_text("interaction/playbackContext")
                + "\n"
                + json.dumps(playback_context(), ensure_ascii=False)
            )
        if hasattr(bridge, "scheduler_snapshot"):
            return bridge.request_response(
                instructions,
                priority=priority,
                interrupt=interrupt,
                speech_policy=speech_policy,
                reason=reason,
                max_protection_ms=max_protection_ms,
                output_mode=output_mode,
                purpose=purpose,
                origin=origin,
            )
        return bridge.request_response(
            instructions, priority=priority, interrupt=interrupt
        )

    async def start_reminder_workflow(
        owner: CaseRuntimeOwner, value: ReminderWorkflowInput
    ) -> None:
        ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
        coordinator = reminder_runtime.get("coordinator")
        try:
            if coordinator is None:
                raise RuntimeError("Temporal reminder coordinator is unavailable")
            await coordinator.start(value)
            async with owner.mutation_lock:
                current = ledger.jobs().get(value.job_id)
                if current and current.get("status") == "queued":
                    ledger.set_job_status(
                        value.job_id,
                        "running",
                        detail={
                            "workflowId": value.workflow_id,
                            "executor": "temporal",
                        },
                    )
        except Exception as exc:
            async with owner.mutation_lock:
                current = ledger.jobs().get(value.job_id)
                if current and current.get("status") not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    ledger.set_job_status(
                        value.job_id,
                        "failed",
                        detail={"error": str(exc), "executor": "temporal"},
                    )
                intent = ledger.intents().get(value.intent_id)
                if intent and intent.get("status") not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    ledger.set_intent_status(
                        value.intent_id, "failed", detail={"error": str(exc)}
                    )
                ledger.enqueue_result(
                    value.intent_id,
                    {
                        "kind": "reminder-failed",
                        "label": value.label,
                        "error": str(exc),
                    },
                    job_id=value.job_id,
                    priority=100,
                    result_id=value.result_id,
                )

    def queue_reminder(
        owner: CaseRuntimeOwner,
        *,
        seconds: int,
        label: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        if reminder_runtime.get("status") != "ready":
            raise RuntimeError(
                reminder_runtime.get("reason") or "Temporal reminder is unavailable"
            )
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 1 <= seconds <= 86400
        ):
            raise ValueError("timer seconds must be an integer between 1 and 86400")
        normalized_label = str(label or "Reminder").strip()[:80] or "Reminder"
        ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
        source_turn_id = str(turn_id or "")
        if source_turn_id not in ledger.turns():
            captured = ledger.capture_turn(
                f"Set reminder: {normalized_label}",
                source="reminder-command",
            )
            source_turn_id = captured["turnId"]
        stable = uuid4().hex[:16]
        workflow_id = f"egoassist-reminder-{owner.session_id}-{stable}"
        result_id = f"reminder-result-{stable}"
        intent = ledger.create_intent(
            source_turn_id,
            kind="background-reminder",
            payload={"label": normalized_label, "seconds": seconds},
            skill_ref={"name": "kitchen-coach", "tool": "set_timer"},
            status="running-background",
            intent_id=f"reminder-intent-{stable}",
        )
        due_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        job = ledger.create_job(
            intent["intentId"],
            kind="timer-delivery",
            payload={
                "dueAt": due_at.isoformat(),
                "label": normalized_label,
                "workflowId": workflow_id,
                "resultId": result_id,
            },
            status="queued",
            job_id=f"reminder-job-{stable}",
        )
        value = ReminderWorkflowInput(
            workflow_id=workflow_id,
            user_id=owner.user_id,
            session_id=owner.session_id,
            journal_path=str(owner.journal.path),
            intent_id=intent["intentId"],
            job_id=job["jobId"],
            result_id=result_id,
            label=normalized_label,
            due_at=due_at.isoformat(),
        )
        task = asyncio.create_task(
            start_reminder_workflow(owner, value),
            name=f"egoassist-start-reminder:{workflow_id}",
        )
        reminder_start_tasks.add(task)
        task.add_done_callback(reminder_start_tasks.discard)
        return {
            "status": "queued",
            "jobId": job["jobId"],
            "intentId": intent["intentId"],
            "workflowId": workflow_id,
            "dueAt": due_at.isoformat(),
            "label": normalized_label,
        }

    def recover_case_reminders(owner: CaseRuntimeOwner) -> None:
        if reminder_runtime.get("status") != "ready":
            return
        ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
        for job in ledger.jobs().values():
            payload = dict(job.get("payload") or {})
            if job.get("kind") != "timer-delivery" or job.get("status") != "queued":
                continue
            intent_id = str(job.get("intentId", ""))
            workflow_id = str(payload.get("workflowId", ""))
            result_id = str(payload.get("resultId", ""))
            if not intent_id or not workflow_id or not result_id:
                continue
            value = ReminderWorkflowInput(
                workflow_id=workflow_id,
                user_id=owner.user_id,
                session_id=owner.session_id,
                journal_path=str(owner.journal.path),
                intent_id=intent_id,
                job_id=str(job["jobId"]),
                result_id=result_id,
                label=str(payload.get("label", "Reminder")),
                due_at=str(payload.get("dueAt")),
            )
            task = asyncio.create_task(
                start_reminder_workflow(owner, value),
                name=f"egoassist-recover-reminder:{workflow_id}",
            )
            reminder_start_tasks.add(task)
            task.add_done_callback(reminder_start_tasks.discard)

    def ensure_job_scheduler(owner: CaseRuntimeOwner) -> None:
        """Start one durable case scheduler independently of WebSocket lifetime."""
        if not platform_capability_available("background-execution"):
            return
        active = owner.job_scheduler_task
        if active is not None and not active.done():
            return

        async def run() -> None:
            ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
            while True:
                for job in ledger.due_jobs():
                    intent_id = str(job.get("intentId", ""))
                    if intent_id not in ledger.intents():
                        continue
                    result = ledger.enqueue_result(
                        intent_id,
                        {
                            "kind": "scheduled-job",
                            "jobId": job["jobId"],
                            "label": (job.get("payload") or {}).get("label", ""),
                        },
                        job_id=job["jobId"],
                        priority=90,
                        allow_stale=True,
                    )
                    ledger.set_job_status(
                        job["jobId"],
                        "completed",
                        detail={
                            "resultId": result["resultId"],
                            "trigger": "durable-scheduler",
                        },
                    )
                bridge = owner.bridge
                if bridge is not None and hasattr(bridge, "scheduler_snapshot"):
                    snapshot = bridge.scheduler_snapshot()
                    unresolved_input = bool(
                        hasattr(bridge, "has_unresolved_user_input")
                        and bridge.has_unresolved_user_input()
                    )
                    if (
                        not unresolved_input
                        and not snapshot.get("pending")
                        and not snapshot.get("activeResponseIds")
                        and not snapshot.get("queued")
                    ):
                        async with owner.mutation_lock:
                            if (
                                owner.bridge is not bridge
                                or owner.socket is None
                                or owner.transitioning
                            ):
                                claimed = None
                            else:
                                claimed = ledger.claim_next_result(
                                    latest_sequence=owner.latest_input_sequence,
                                )
                            if claimed is not None:
                                correlation = dict(claimed.get("correlation") or {})
                                if correlation.get("sessionId") not in {
                                    None,
                                    owner.session_id,
                                }:
                                    ledger.discard_result(
                                        claimed["resultId"],
                                        reason="cross-session-result",
                                    )
                                    claimed = None
                            if claimed is not None:
                                accepted = request_bridge_response(
                                    bridge,
                                    "Deliver this completed background result naturally and briefly. "
                                    "Do not mention internal ledgers: "
                                    + json.dumps(
                                        claimed.get("content"), ensure_ascii=False
                                    ),
                                    priority=int(claimed.get("priority", 50)),
                                    reason="result-inbox-arbiter",
                                    origin={
                                        "sessionId": (
                                            claimed.get("correlation") or {}
                                        ).get("sessionId", owner.session_id),
                                        "turnId": (
                                            claimed.get("correlation") or {}
                                        ).get("turnId"),
                                        "intentId": claimed.get("intentId"),
                                        "jobId": claimed.get("jobId"),
                                        "resultId": claimed.get("resultId"),
                                        "generation": (
                                            claimed.get("correlation") or {}
                                        ).get("generation"),
                                        "background": True,
                                    },
                                )
                                if not accepted:
                                    ledger.set_result_status(
                                        claimed["resultId"],
                                        "pending",
                                        detail={
                                            "reason": "speech-arbiter-not-accepted"
                                        },
                                    )
                await asyncio.sleep(0.25)

        task = asyncio.create_task(
            run(), name=f"egoassist-job-scheduler:{owner.session_id}"
        )
        owner.job_scheduler_task = task

    async def stop_case_connection(
        owner: CaseRuntimeOwner, *, notify: bool = True
    ) -> None:
        if owner.scene and notify:
            await owner.scene.close()
        ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
        for turn in ledger.snapshot()["turnInbox"]:
            delivery = turn.get("delivery") or {}
            if delivery.get("status") == "responding" or (
                delivery.get("status") == "received"
                and turn.get("status") == "completed"
            ):
                ledger.set_turn_delivery(
                    turn["turnId"], "interrupted", reason="connection-closed"
                )
                answer_executor.interrupt(owner.journal, turn["turnId"])
        owner.epoch += 1
        socket = owner.socket
        owner.socket_token = None
        owner.socket = None
        owner.bridge = None
        worker = owner.turn_worker
        owner.turn_worker = None
        queue = owner.turn_queue
        owner.turn_queue = None
        if worker and worker is not asyncio.current_task() and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        classification_tasks = tuple(owner.classification_tasks)
        owner.classification_tasks.clear()
        for task in classification_tasks:
            if not task.done():
                task.cancel()
        if classification_tasks:
            await asyncio.gather(*classification_tasks, return_exceptions=True)
        prepared = tuple(owner.prepared_scenes.values())
        owner.prepared_scenes.clear()
        owner.applied_controls.clear()
        owner.pending_semantic_sequences.clear()
        for task in prepared:
            if not task.done():
                task.cancel()
        if prepared:
            await asyncio.gather(*prepared, return_exceptions=True)
        if queue:
            while not queue.empty():
                try:
                    queued = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                future = queued.get("future") if isinstance(queued, dict) else None
                if future and not future.done():
                    future.set_exception(
                        RuntimeError(
                            "case runtime changed before the turn was processed"
                        )
                    )
                queue.task_done()
        if (
            socket is not None
            and getattr(socket, "prepared", False)
            and not socket.closed
        ):
            if notify:
                await socket.send_json({"type": "session_stale"})
            await socket.close(code=1000, message=b"task context changed")

    @asynccontextmanager
    async def exclusive_case_transition(owner: CaseRuntimeOwner):
        async with owner.mutation_lock:
            owner.transitioning = True
            try:
                await stop_case_connection(owner)
                yield
            finally:
                owner.transitioning = False

    async def invalidate_live_sessions(
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if session_id:
            owner = case_owners.get(session_id)
            if owner is not None:
                await stop_case_connection(owner)
                return
            sockets = tuple(live_sockets_by_session.get(session_id, set()))
        elif user_id:
            owners = [
                owner for owner in case_owners.values() if owner.user_id == user_id
            ]
            for owner in owners:
                await stop_case_connection(owner)
            sockets = tuple(live_sockets_by_user.get(user_id, set()))
        else:
            for owner in tuple(case_owners.values()):
                await stop_case_connection(owner)
            sockets = tuple(live_sockets)
        for socket in sockets:
            if not socket.closed:
                await socket.send_json({"type": "session_stale"})
                await socket.close(code=1000, message=b"task context changed")

    def control_instructions(
        target_runtime: SkillRuntime | None = None,
        *,
        task_control: str | None = None,
    ) -> str:
        active_runtime = target_runtime or runtime
        if active_runtime.task is None:
            if active_runtime.skill:
                if not active_runtime.tasks:
                    return (
                        f"The active Skill is {active_runtime.skill.name}, a conversational workflow without fixed steps. "
                        "Apply its policy to the current request directly; do not ask the user to select a task."
                    )
                return f"The active Skill is {active_runtime.skill.name}, but the task is unresolved."
            return "Respond naturally to the current user turn already present in conversation data."
        if active_runtime.task.completed:
            return (
                "The user has confirmed all task checkpoints complete. Briefly acknowledge their confirmation. "
                "Do not invent another checkpoint, an extra checklist or a mandatory final check. "
                "This records user confirmation, not independently verified physical completion."
            )
        decision = active_runtime.task.decision
        if task_control == "repeat":
            return PROMPTS.task_repeat
        if task_control == "request-help":
            return PROMPTS.task_request_help
        if task_control == "confirm-step":
            return PROMPTS.task_confirm_step.format(message=decision["message"])
        if task_control == "rollback":
            return PROMPTS.task_rollback
        if task_control == "pause-task":
            return PROMPTS.task_pause
        if task_control == "resume-task":
            return PROMPTS.task_resume
        return (
            "Respond to the current user control request already present in conversation data. "
            f"The authoritative current checkpoint is {active_runtime.task.current_step.id}: "
            f"{active_runtime.task.current_step.instruction}. "
            "No task transition was accepted for this turn. Do not claim the step was marked complete "
            "or guide the next step; if the user intended completion, ask them to confirm the current step. "
            + PROMPTS.task_general
        )

    def grounded_user_instructions(
        transcript: str,
        result: dict,
        target_runtime: SkillRuntime | None = None,
        *, clock=None,
    ) -> str:
        current_turn = json.dumps(str(transcript).strip(), ensure_ascii=False)
        instructions = (
            prompt_text("run_egoassist/grounded_user_instructions.1", v0=current_turn)
        ) + control_instructions(
            target_runtime,
            task_control=result.get("taskControl"),
        )
        instructions += prompt_text("run_egoassist/grounded_user_instructions.2")
        instructions += prompt_text("interaction/responsive_answer")
        from egoassist.product.answer_time import localize_evidence, time_instructions
        instructions += time_instructions(clock)
        active_runtime = target_runtime or runtime
        observation = active_runtime.task.observation if active_runtime.task else None
        shared = result.get("sharedScene")
        if shared:
            from egoassist.realtime.interaction_window import answer_scene_context
            if shared.get('interactionWindow'):
                instructions += prompt_text('scene/temporal_answer')
            instructions += prompt_text("scene/grounding") + json.dumps(
                localize_evidence(answer_scene_context(shared), clock), ensure_ascii=False
            )
            if not shared.get("speaker", {}).get("wearer"):
                instructions += prompt_text("scene/unverified")
        elif (
            not observation
            or not str(observation.get("source", "")).startswith("camera/")
            or not observation.get("frameId")
        ):
            instructions += prompt_text("run_egoassist/grounded_user_instructions.3")
        else:
            instructions += prompt_text("run_egoassist/grounded_user_instructions.4")
        evidence = result.get("episodicInjection", {}).get("evidence", [])
        packet = result.get("evidencePackage")
        if result.get('memoryRead', {}).get('status') == 'degraded':
            instructions += prompt_text('evidence_answer/unavailable')
        if packet:
            from egoassist.product.answer_service import evidence_instructions
            instructions += '\n' + evidence_instructions(packet, clock=clock)
            evidence = evidence + packet.get('sources', []) + packet.get('conversation', [])
        if (
            result.get("episodicInjection", {}).get("temporalStatus")
            == "needs-clarification"
            and not packet
        ):
            instructions += prompt_text("run_egoassist/grounded_user_instructions.5")
        if evidence:
            instructions += prompt_text("run_egoassist/grounded_user_instructions.6")
        elif result.get("episodicRetrieval", {}).get("referential"):
            instructions += PROMPTS.grounding_referential_no_evidence
        else:
            instructions += PROMPTS.grounding_no_evidence
        if result.get("memoryRecalled"):
            instructions += prompt_text("run_egoassist/grounded_user_instructions.7")
        intent_execution = (result.get("semanticDecision") or {}).get(
            "intentExecution"
        ) or {}
        if intent_execution.get("status") == "unsupported":
            instructions += prompt_text("run_egoassist/grounded_user_instructions.8")
        elif intent_execution.get("status") == "waiting-input":
            instructions += prompt_text("run_egoassist/grounded_user_instructions.9")
        elif intent_execution.get("status") == "queued":
            accepted = list(intent_execution.get("accepted") or [])
            instructions += (
                prompt_text("run_egoassist/grounded_user_instructions.10")
                + json.dumps(accepted, ensure_ascii=False)
                + prompt_text("run_egoassist/grounded_user_instructions.11")
            )
        timeline_write = result.get("timelineWrite") or {}
        card = timeline_write.get("card") or {}
        committed_card_ids = list(timeline_write.get("committedCardIds") or [])
        if committed_card_ids:
            instructions += prompt_text("run_egoassist/grounded_user_instructions.12")
        elif timeline_write.get("action") == "capture":
            instructions += prompt_text("run_egoassist/grounded_user_instructions.13")
        elif card.get("status") == "pending":
            instructions += prompt_text("run_egoassist/grounded_user_instructions.14")
        else:
            instructions += prompt_text("run_egoassist/grounded_user_instructions.15")
        if result.get("referenceResult"):
            reference_result = result["referenceResult"]
            instructions += prompt_text(
                "run_egoassist/grounded_user_instructions.16"
            ) + json.dumps(
                {
                    "status": reference_result.get("status"),
                    "title": (reference_result.get("artifact") or {}).get("title"),
                },
                ensure_ascii=False,
            )
        elif intent_execution.get("status") != "queued":
            instructions += prompt_text("run_egoassist/grounded_user_instructions.17")
        return instructions

    def request_decision_speech(decision: dict, priority: int | None = None) -> None:
        if decision.get("action") not in {"guide", "ask", "emergency"}:
            return
        request_priority = (
            priority
            if priority is not None
            else (100 if decision["action"] == "emergency" else 70)
        )
        instructions = PROMPTS.decision_speech.format(
            message=decision["message"], reason=decision["reason"]
        )
        for bridge in tuple(live_bridges):
            bridge.update_context(context())
            selection = turn_policy().select(decision)
            request_bridge_response(
                bridge,
                instructions,
                priority=request_priority,
                interrupt=decision["action"] == "emergency",
                speech_policy=selection["policy"],
                reason=f"{selection['basis']}:{decision['action']}",
                max_protection_ms=selection["maxProtectionMs"]
                or turn_policy().max_protection_ms,
            )

    def request_memory_speech(
        hint: dict | None,
        *,
        target_bridge=None,
        target_runtime: SkillRuntime | None = None,
    ) -> None:
        if not hint:
            return
        instructions = (
            prompt_text("run_egoassist/request_memory_speech.1") + PROMPTS.memory_speech
        )
        bridges = (target_bridge,) if target_bridge is not None else tuple(live_bridges)
        for bridge in bridges:
            bridge.update_context(context(target_runtime))
            if hasattr(bridge, "inject_context_data"):
                bridge.inject_context_data(
                    "confirmed_preference",
                    {
                        "memoryId": hint.get("id"),
                        "prompt": hint.get("prompt"),
                    },
                )
            request_bridge_response(
                bridge,
                instructions,
                priority=80,
                speech_policy="immediate",
                reason="preference-memory-recall",
            )

    def scoped_state_for_user(
        user_id: str,
        session_id: str | None = None,
        *,
        runtime_override: SkillRuntime | None = None,
        routing_override: dict | None = None,
        tools_override: SkillToolExecutor | None = None,
    ) -> dict:
        if session_id:
            owner = case_owner(user_id, session_id)
            active_runtime = runtime_override or owner.runtime
            active_routing = routing_override or owner.routing
            active_tools = tools_override or owner.tools
            active_harness = owner.harness
        else:
            active_runtime = runtime_override or runtime_for_user(user_id)
            active_routing = routing_override or user_routing[user_id]
            active_tools = tools_override or user_tools[user_id]
            active_harness = None
        policy = (
            identity.get_context_session(user_id, session_id)["memoryPolicy"]
            if session_id
            else default_memory_policy()
        )
        return state(
            active_runtime,
            routing_override=active_routing,
            harness_override=active_harness,
            tools_override=active_tools,
            episodic_override=scoped_episodic_memory(user_id, session_id, policy),
            user_scope=user_id,
            memory_policy_override=policy,
            session_id_override=session_id,
        )

    async def get_state(request):
        user_id = request[user_key]["id"]
        session_id = str(request.query.get("session", "")).strip() or None
        return web.json_response(scoped_state_for_user(user_id, session_id))

    async def get_index(request):
        return web.FileResponse(WEB_ROOT / "index.html")

    def record_memory_operation(session_id, operation, **payload):
        owner = case_owners.get(session_id)
        if owner and owner.memory_bus:
            owner.memory_bus.record(operation, **payload)

    memory_commit = MemoryCommitService(
        append_memory_audit=append_memory_audit,
        record_operation=record_memory_operation,
        episodic_for_user=episodic_for_user,
        memory_recovery=memory_recovery,
        timeline=timeline,
    )

    memory_service = MemoryService(timeline, evidence_service, memory_commit,
        read_timeout=number("EGOASSIST_MEMORY_READ_TIMEOUT", 2, minimum=0.05, maximum=30))

    async def memory_reads_context(_app):
        yield
        await evidence_service.close()

    app_memory_reads_context = memory_reads_context
    interaction_service = InteractionService(
        append_memory_audit=append_memory_audit,
        apply_memory_hint=apply_memory_hint,
        case_owners=case_owners,
        default_interaction=default_interaction,
        episodic=episodic,
        episodic_for_user=episodic_for_user,
        find_memory_hint=find_memory_hint,
        live_bridges=live_bridges,
        memory_service=memory_service,
        memory_retrieval=memory_retrieval,
        registry=registry,
        runtime=runtime,
        runtime_checkpoint=runtime_checkpoint,
        runtime_context=runtime_context,
        trace_event=trace_event,
    )
    process_transcript = interaction_service.process_transcript

    def validate_external_observation(
        active_runtime: SkillRuntime,
        payload: dict[str, Any],
        *,
        user_id: str,
        session_id: str,
    ):
        captured_at = datetime.now(timezone.utc)
        raw_captured_at = payload.get("frameCapturedAt") or payload.get("observedAt")
        if raw_captured_at:
            try:
                captured_at = datetime.fromisoformat(
                    str(raw_captured_at).replace("Z", "+00:00")
                )
                if captured_at.tzinfo is None:
                    captured_at = captured_at.replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ValueError("invalid observation capture timestamp") from exc
        frame_id = str(payload.get("frameId") or f"manual-frame-{uuid4().hex}")
        context = perception_context_for_runtime(
            active_runtime,
            user_id=user_id,
            session_id=session_id,
            frame_id=frame_id,
            captured_at=captured_at,
        )
        if context is None:
            raise ValueError("no active task for structured observation")
        raw_hazards = payload.get("hazards", [])
        if not isinstance(raw_hazards, list):
            raise ValueError("observation hazards must be a list")
        hypothesis = PerceptionHypothesis.model_validate(
            {
                "checkpoint_id": str(
                    payload.get("step") or payload.get("checkpointId") or "unknown"
                ),
                "completed": bool(payload.get("completed", False)),
                "confidence": payload.get("confidence", 0),
                "summary": str(payload.get("summary", "")),
                "visible_evidence": list(payload.get("visibleEvidence", [])),
                "hazards": [
                    {
                        "kind": str(
                            item.get("kind") or item.get("type") or "task-safety"
                        ),
                        "message": str(item.get("message", "")),
                        "confidence": item.get("confidence", 0),
                    }
                    for item in raw_hazards
                    if isinstance(item, dict)
                ],
                "unrelated": bool(payload.get("unrelated", False)),
            }
        )
        current = perception_context_for_runtime(
            active_runtime,
            user_id=user_id,
            session_id=session_id,
            frame_id=frame_id,
            captured_at=captured_at,
        )
        validation = validate_perception_result(
            context,
            current,
            hypothesis,
            source=f"manual-external/{str(payload.get('source', 'unspecified'))[:60]}",
        )
        if not validation.accepted or validation.observation is None:
            active_runtime.task.record_perception_rejection(
                validation.reason,
                frame_id=frame_id,
                context_id=context.context_id,
                detail={"channel": "http-manual-observation"},
            )
            raise ValueError(f"observation rejected: {validation.reason}")
        return validation.observation

    def ambient_status(user_id: str) -> dict[str, Any]:
        """Truthful ambient state for the console: enabled, consented, watching what."""
        grant = ambient_consent(user_id) if ambient_settings["enabled"] else None
        watchlist = ambient_settings["watchlist"]
        return {
            "enabled": bool(ambient_settings["enabled"]),
            "reason": ambient_settings["reason"],
            "consented": grant is not None,
            "consentId": grant["consent_id"] if grant else None,
            "controlRevision": ambient_control_revisions.get(user_id, 0),
            "controlEpoch": ambient_control_epoch,
            "consentScope": "account",
            "expiresAt": grant.get("expires_at") if grant else None,
            "intervalSeconds": ambient_settings["interval_seconds"],
            "watchlist": watchlist.public_summary() if watchlist is not None else None,
        }

    def ambient_snapshot(user_id, session_id):
        owner = case_owner(user_id, session_id)
        watch = ambient_watch_session(owner)
        snapshot = {
            **ambient_status(user_id),
            **(watch.snapshot() if watch else {}),
            **ambient_options(owner),
        }
        bridge = owner.bridge
        lane = getattr(bridge, "ambient_perception", None)
        latest = getattr(bridge, "_latest_perception_frame", None)
        snapshot["processing"] = bool(lane and lane.active)
        snapshot["connected"] = bool(bridge and getattr(bridge, "connected", False))
        snapshot["cameraAt"] = (
            latest.get("capturedAt").isoformat() if isinstance(latest, dict) else None
        )
        snapshot["taskActive"] = owner.runtime.task is not None
        snapshot["lane"] = lane.snapshot() if lane else None
        snapshot["taskLane"] = (
            bridge.perception.snapshot()
            if bridge and hasattr(bridge, "perception")
            else None
        )
        snapshot["elapsedMs"] = (
            round((time.monotonic() - lane._request_started) * 1000)
            if lane and lane.active and lane._request_started
            else None
        )
        return snapshot

    async def publish_ambient_control(user_id, session_id=None):
        """Push authoritative control state to every connected session of the account."""
        ambient_control_revisions[user_id] = ambient_control_revisions.get(user_id, 0) + 1
        for owner in list(case_owners.values()):
            if owner.user_id != user_id:
                continue
            payload = {"type": "ambient", "ambient": ambient_snapshot(user_id, owner.session_id)}
            if livekit_transport is not None:
                info = payload["ambient"]
                livekit_transport.set_observation_speech_allowed(
                    owner.session_id, bool(info.get("consented")),
                    kinds=({"clarification"} if info.get("questionsEnabled") else set()) |
                          ({"reminder"} if info.get("remindersEnabled") else set()))
            for ws in list(live_sockets_by_session.get(owner.session_id, ())):
                if not ws.closed:
                    try:
                        await ws.send_json(payload)
                    except (ConnectionError, RuntimeError):
                        pass
        result = {"ambient": ambient_snapshot(user_id, session_id) if session_id else ambient_status(user_id)}
        if session_id:
            result["state"] = scoped_state_for_user(user_id, session_id)
        return web.json_response(result)

    async def post_ambient_consent(request):
        user_id = request[user_key]["id"]
        async with ambient_control_locks.setdefault(user_id, asyncio.Lock()):
            return await change_ambient_consent(request)

    async def change_ambient_consent(request):
        """Grant or revoke consent for ambient observation.

        Ambient mode observes nothing without a live grant here, and revoking
        invalidates frames already in flight because the consent id is part of
        every frame's binding tuple.
        """
        user_id = request[user_key]["id"]
        payload = await request.json() if request.can_read_body else {}
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            identity.get_context_session(user_id, session_id)
        if payload.get("granted") is True:
            if not ambient_settings["enabled"]:
                raise ValueError(
                    "ambient observation is not enabled on this deployment"
                )
            watchlist = ambient_settings["watchlist"]
            if ambient_consent(user_id) is None:
                product_database.record_consent_grant(
                    user_id,
                    purpose=AMBIENT_CONSENT_PURPOSE,
                    data_categories=[
                        "periodically sampled video frames",
                        "watchlist activity and location labels",
                    ],
                    destination="ambient-observation-model",
                )
            trace_event(
                "ambient.consent.granted",
                "ambient-policy",
                {
                    "userId": user_id,
                    "watchlistId": watchlist.id if watchlist else None,
                },
            )
        elif payload.get("granted") is False:
            grant = ambient_consent(user_id)
            if grant is not None:
                product_database.revoke_consent(grant["consent_id"])
            stopping = []
            for owner in list(case_owners.values()):
                if owner.user_id == user_id:
                    owner.ambient = None
                    if livekit_transport is not None:
                        livekit_transport.set_observation_speech_allowed(owner.session_id, False)
                    bridge = owner.bridge
                    if bridge and hasattr(bridge, "stop_ambient_observation"):
                        stopping.append(bridge.stop_ambient_observation())
            if stopping:
                await asyncio.gather(*stopping)
            trace_event(
                "ambient.consent.revoked", "ambient-policy", {"userId": user_id}
            )
        else:
            raise ValueError("granted must be true or false")
        return await publish_ambient_control(user_id, session_id)

    async def post_resume_speech(request):
        payload = await request.json()
        user_id = request[user_key]['id']
        session_id = str(payload.get('sessionId', ''))
        identity.get_context_session(user_id, session_id)
        permission_id = payload.get('permissionId')
        if not isinstance(permission_id, str) or not permission_id:
            raise ValueError('permissionId is required')
        owner = case_owner(user_id, session_id)
        async with owner.mutation_lock:
            if permission_id != owner.interaction.permission.get('id'):
                return web.json_response({'error': '播报设置已变化，请刷新状态后再操作。'}, status=409)
            owner.interaction.clear_quiet(permission_id)
        current = scoped_state_for_user(user_id, session_id)
        for ws in list(live_sockets_by_session.get(session_id, ())):
            if not ws.closed:
                try:
                    await ws.send_json({'type': 'state', 'state': current})
                except (ConnectionError, RuntimeError):
                    pass
        return web.json_response({'state': current})

    async def post_ambient_settings(request):
        user_id = request[user_key]["id"]
        async with ambient_control_locks.setdefault(user_id, asyncio.Lock()):
            return await change_ambient_settings(request)

    async def change_ambient_settings(request):
        payload = await request.json()
        user_id = request[user_key]["id"]
        session_id = str(payload.get("sessionId", ""))
        identity.get_context_session(user_id, session_id)
        owner = case_owner(user_id, session_id)
        interval = payload.get("intervalSeconds")
        questions = payload.get("questionsEnabled")
        previous = ambient_options(owner)
        reminders = payload.get("remindersEnabled", previous["remindersEnabled"])
        goal = payload.get("reminderGoal", previous["reminderGoal"])
        if not isinstance(reminders, bool) or not isinstance(goal, str) or len(goal) > 600:
            raise ValueError("remindersEnabled must be boolean and reminderGoal at most 600 characters")
        cooldown = payload.get(
            "questionCooldownSeconds", ambient_options(owner)["questionCooldownSeconds"]
        )
        if (
            type(interval) not in {int, float}
            or not 2 <= interval <= 120
            or not isinstance(questions, bool)
        ):
            raise ValueError(
                "intervalSeconds must be 2-120 and questionsEnabled must be boolean"
            )
        if type(cooldown) not in {int, float} or not 1 <= cooldown <= 3600:
            raise ValueError("questionCooldownSeconds must be 1-3600")
        async with owner.mutation_lock:
            settings = {
                "intervalSeconds": float(interval),
                "questionsEnabled": questions,
                "remindersEnabled": reminders,
                "reminderGoal": goal.strip(),
                "questionCooldownSeconds": float(cooldown),
            }
            owner.journal.append("ambient.settings", {"settings": settings})
            owner.ambient_options = settings
        disabled_kinds = ({"clarification"} if not questions else set()) | ({"reminder"} if not reminders else set())
        if goal.strip() != previous["reminderGoal"]:
            disabled_kinds.add("reminder")
        if disabled_kinds and livekit_transport is not None:
            livekit_transport.set_observation_speech_allowed(
                session_id, bool(ambient_consent(user_id)), kinds={"reminder", "clarification"} - disabled_kinds)
        if disabled_kinds and owner.bridge and hasattr(owner.bridge, "cancel_observation_questions"):
            owner.bridge.cancel_observation_questions(kinds=disabled_kinds)
        return await publish_ambient_control(user_id, session_id)

    def submit_tool_confirmation(owner, call_id, accepted):
        if not isinstance(accepted, bool):
            raise ValueError("accepted must be a boolean")
        if call_id in owner.tool_confirmation_tasks:
            return
        if any(
            e.get("callId") == call_id
            and e.get("type") in {"tool_result", "tool_execution"}
            for e in owner.journal.entries(types={"tool_result", "tool_execution"})
        ):
            return
        if call_id not in owner.journal.pending_tool_confirmations():
            raise web.HTTPNotFound(text="pending tool call not found")
        bridge = owner.bridge
        if not bridge or not getattr(bridge, "connected", False):
            raise web.HTTPConflict(
                text="Reconnect the assistant to confirm this tool call"
            )

        async def execute():
            try:
                await asyncio.to_thread(bridge.confirm_tool, call_id, accepted)
            except Exception:
                logging.exception("Tool confirmation failed for %s", call_id)
            finally:
                owner.tool_confirmation_tasks.pop(call_id, None)

        owner.tool_confirmation_tasks[call_id] = asyncio.create_task(execute())

    async def post_livekit_token(request):
        if livekit_transport is None or livekit_settings is None:
            return web.json_response(
                {
                    "enabled": False,
                    "transport": "websocket-fallback",
                    "capability": livekit_capability(None),
                }
            )
        user_id = request[user_key]["id"]
        payload = await request.json()
        requested_session_id = str(payload.get("sessionId", "")).strip()
        session_id = identity.claim_context_session(
            user_id, requested_session_id or None
        )
        participant_id = f"participant-{user_id}-{uuid4().hex[:10]}"
        return web.json_response(
            {
                "enabled": True,
                "transport": "livekit",
                "url": livekit_settings.url,
                "room": livekit_transport.room_name(session_id),
                "token": livekit_transport.issue_browser_token(
                    session_id, participant_id
                ),
                "participantId": participant_id,
                "expiresInSeconds": livekit_settings.token_ttl_seconds,
            }
        )

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except CaseOwnerCapacityError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except web.HTTPException:
            raise
        except Exception:
            request_id = uuid4().hex[:10]
            LOGGER.exception(
                "request %s failed: %s %s", request_id, request.method, request.path
            )
            return web.json_response(
                {
                    "error": "internal_error",
                    "message": PROMPTS.error_internal,
                    "requestId": request_id,
                },
                status=500,
            )

    public_api_paths = {
        "/api/auth/register",
        "/api/auth/login",
        "/api/auth/me",
    }

    @web.middleware
    async def authentication(request, handler):
        user = identity.authenticate(request.cookies.get(AUTH_COOKIE))
        request[user_key] = user
        if user and user["id"] in deleting_insight_users:
            return web.json_response(
                {"error": "account data maintenance in progress"}, status=503
            )
        protected = request.path == "/ws/realtime" or request.path.startswith("/api/")
        if protected and request.path not in public_api_paths and user is None:
            return web.json_response({"error": "authentication required"}, status=401)
        if user is None:
            return await handler(request)
        task = asyncio.current_task()
        active_account_requests.setdefault(user["id"], set()).add(task)
        try:
            return await handler(request)
        finally:
            active_account_requests.get(user["id"], set()).discard(task)

    app = web.Application(
        client_max_size=3_000_000, middlewares=[errors, authentication]
    )

    async def temporal_reminder_context(_app):
        target = str(os.getenv("EGOASSIST_TEMPORAL_TARGET", "")).strip()
        if not target:
            yield
            return
        worker = None
        worker_task = None
        try:
            coordinator = await TemporalReminderCoordinator.connect(
                target,
                namespace=os.getenv("EGOASSIST_TEMPORAL_NAMESPACE", "default"),
                task_queue=os.getenv(
                    "EGOASSIST_TEMPORAL_TASK_QUEUE", DEFAULT_TASK_QUEUE
                ),
            )
            notification = None
            ntfy_topic = str(os.getenv("EGOASSIST_NTFY_TOPIC", "")).strip()
            if ntfy_topic:
                notification = NtfyNotificationAdapter(
                    ntfy_topic,
                    server=os.getenv("EGOASSIST_NTFY_SERVER", "https://ntfy.sh"),
                    token=os.getenv("EGOASSIST_NTFY_TOKEN"),
                )
            activities = ReminderActivities(notification)
            worker = Worker(
                coordinator.client,
                task_queue=coordinator.task_queue,
                workflows=[ReminderWorkflow],
                activities=[activities.deliver],
            )
            worker_task = asyncio.create_task(
                worker.run(), name="egoassist-temporal-reminder-worker"
            )
            reminder_runtime.update(
                {
                    "status": "ready",
                    "coordinator": coordinator,
                    "worker": worker,
                    "workerTask": worker_task,
                    "reason": "Temporal workflow and durable ResultInbox delivery are connected.",
                }
            )
            capability_overrides["timer-delivery"] = {
                "status": "available",
                "reason": "Temporal owns timer lifecycle; ResultInbox is durable and ntfy is optional.",
            }
            for owner in tuple(case_owners.values()):
                recover_case_reminders(owner)
        except Exception as exc:
            LOGGER.exception("Temporal reminder startup failed")
            reminder_runtime.update(
                {
                    "status": "unavailable",
                    "coordinator": None,
                    "reason": f"Temporal connection failed: {exc}",
                }
            )
        try:
            yield
        finally:
            capability_overrides.pop("timer-delivery", None)
            reminder_runtime["status"] = "stopping"
            if reminder_start_tasks:
                await asyncio.gather(
                    *tuple(reminder_start_tasks), return_exceptions=True
                )
            if worker is not None:
                await worker.shutdown()
            if worker_task is not None:
                await asyncio.gather(worker_task, return_exceptions=True)
            reminder_runtime.update({"status": "stopped", "coordinator": None})

    async def runtime_root_lease_context(_app):
        lease = RuntimeRootLease(memory_path)
        lease.acquire()
        try:
            await asyncio.to_thread(privacy_governance.retention)
            yield
        finally:
            try:
                await invalidate_live_sessions()
                maintenance = [
                    owner.maintenance_task
                    for owner in case_owners.values()
                    if owner.maintenance_task is not None
                    and not owner.maintenance_task.done()
                ]
                if maintenance:
                    await asyncio.gather(*maintenance, return_exceptions=True)
                schedulers = [
                    owner.job_scheduler_task
                    for owner in case_owners.values()
                    if owner.job_scheduler_task is not None
                    and not owner.job_scheduler_task.done()
                ]
                for scheduler in schedulers:
                    scheduler.cancel()
                if schedulers:
                    await asyncio.gather(*schedulers, return_exceptions=True)
            finally:
                if livekit_transport is not None:
                    await livekit_transport.close_all()
                product_database.close()
                lease.release()

    app.cleanup_ctx.append(runtime_root_lease_context)
    app.cleanup_ctx.append(temporal_reminder_context)

    async def insight_jobs_context(_app):
        for path in sorted(user_memory_root.glob("*/sessions/*.jsonl")):
            user_id, session_id = path.parent.parent.name, path.stem
            try:
                identity.get_context_session(user_id, session_id)
                insight_executor.recover(
                    user_id, session_id, case_owner(user_id, session_id).journal
                )
            except (ValueError, KeyError, OSError):
                LOGGER.warning("Skipped unavailable report journal during startup")
        yield
        await insight_executor.close()

    app.cleanup_ctx.append(insight_jobs_context)

    async def answer_jobs_context(_app):
        for path in sorted(user_memory_root.glob("*/sessions/*.jsonl")):
            user_id, session_id = path.parent.parent.name, path.stem
            try:
                identity.get_context_session(user_id, session_id)
                answer_executor.recover(
                    user_id, case_owner(user_id, session_id).journal
                )
            except (ValueError, KeyError, OSError):
                LOGGER.warning("Skipped unavailable answer journal during startup")
        worker = asyncio.create_task(
            answer_executor.run(), name="egoassist-answer-completion"
        )
        try:
            yield
        finally:
            await answer_executor.close()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    app.cleanup_ctx.append(answer_jobs_context)

    async def memory_recovery_context(_app):
        for path in sorted(user_memory_root.glob("*/sessions/*.jsonl")):
            user_id, session_id = path.parent.parent.name, path.stem
            try:
                identity.get_context_session(user_id, session_id)
                memory_recovery.recover(case_owner(user_id, session_id).journal)
            except (ValueError, KeyError, OSError):
                LOGGER.warning("Skipped unavailable memory recovery journal")
        worker = asyncio.create_task(
            memory_recovery.run(), name="egoassist-memory-recovery"
        )
        try:
            yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await memory_recovery.close()

    app.cleanup_ctx.append(memory_recovery_context)
    retention = AccountRetention(
        privacy_governance,
        identity,
        deleting_insight_users,
        invalidate_live_sessions,
        insight_executor,
        answer_executor,
        memory_recovery,
        case_owners,
        case_episodics,
        user_episodics,
        projection_cache,
        active_requests=active_account_requests,
    )

    async def retention_context(_app):
        worker = asyncio.create_task(retention.run(), name="egoassist-retention")
        try:
            yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    app.cleanup_ctx.append(retention_context)
    _stateprojection = StateProjection(
        ambient_snapshot=ambient_snapshot,
        artifacts_for_runtime=artifacts_for_runtime,
        capability_overrides=capability_overrides,
        case_owners=case_owners,
        context=context,
        default_interaction=default_interaction,
        episodic=episodic,
        episodic_for_user=episodic_for_user,
        human_help_port=human_help_port,
        ledger=ledger,
        live_bridges=live_bridges,
        media_configuration=media_configuration,
        memory_retrieval=memory_retrieval,
        model=model,
        product_database=product_database,
        projection_cache=projection_cache,
        runtime=runtime,
        runtime_skill_summaries=runtime_skill_summaries,
        runtime_skill_summary=runtime_skill_summary,
        timeline=timeline,
        tools=tools,
        memory_metrics=memory_service.status,
        memory_service=memory_service,
    )
    state = _stateprojection.state

    _identityendpoints = IdentityEndpoints(
        AUTH_COOKIE=AUTH_COOKIE,
        active_skill=active_skill,
        identity=identity,
        invalidate_live_sessions=invalidate_live_sessions,
        registry=registry,
        runtime_for_user=runtime_for_user,
        user_key=user_key,
        user_routing=user_routing,
        user_runtimes=user_runtimes,
        user_tools=user_tools,
        web=web,
    )
    get_me = _identityendpoints.get_me
    get_sessions = _identityendpoints.get_sessions
    post_login = _identityendpoints.post_login
    post_logout = _identityendpoints.post_logout
    post_register = _identityendpoints.post_register
    post_session = _identityendpoints.post_session

    _memoryendpoints = MemoryEndpoints(
        retention=retention,
        AUTH_COOKIE=AUTH_COOKIE,
        ambient_consent=ambient_consent,
        ambient_settings=ambient_settings,
        ambient_status=ambient_status,
        answer_executor=answer_executor,
        append_memory_audit=append_memory_audit,
        case_episodics=case_episodics,
        case_journal=case_journal,
        case_owner=case_owner,
        case_owners=case_owners,
        clock_for_session=clock_for_session,
        deleting_insight_users=deleting_insight_users,
        identity=identity,
        insight_executor=insight_executor,
        invalidate_live_sessions=invalidate_live_sessions,
        memory_recovery=memory_recovery,
        memory_service=memory_service,
        outcome_ledger=outcome_ledger,
        privacy_governance=privacy_governance,
        product_database=product_database,
        projection_cache=projection_cache,
        scoped_episodic_memory=scoped_episodic_memory,
        scoped_state_for_user=scoped_state_for_user,
        timeline=timeline,
        user_episodics=user_episodics,
        user_key=user_key,
        user_routing=user_routing,
        user_runtimes=user_runtimes,
        user_tools=user_tools,
        web=web,
    )
    get_insights = _memoryendpoints.get_insights
    get_memory_state = _memoryendpoints.get_memory_state
    get_privacy = _memoryendpoints.get_privacy
    get_privacy_export = _memoryendpoints.get_privacy_export
    get_timeline_memory = _memoryendpoints.get_timeline_memory
    post_candidate = _memoryendpoints.post_candidate
    post_delete = _memoryendpoints.post_delete
    post_insight = _memoryendpoints.post_insight
    post_insight_cancel = _memoryendpoints.post_insight_cancel
    post_insight_policy = _memoryendpoints.post_insight_policy
    post_memory_policy = _memoryendpoints.post_memory_policy
    post_memory_recovery_retry = _memoryendpoints.post_memory_recovery_retry
    post_privacy_delete = _memoryendpoints.post_privacy_delete
    post_privacy_retention = _memoryendpoints.post_privacy_retention

    _taskendpoints = TaskEndpoints(
        SKILL_CONTROL_LABELS=SKILL_CONTROL_LABELS,
        TASK_CONTROL_LABELS=TASK_CONTROL_LABELS,
        answer_executor=answer_executor,
        append_case_runtime_checkpoint=append_case_runtime_checkpoint,
        artifacts_for_runtime=artifacts_for_runtime,
        case_owner=case_owner,
        clock_for_session=clock_for_session,
        episodic_for_user=episodic_for_user,
        exclusive_case_transition=exclusive_case_transition,
        get_state=get_state,
        identity=identity,
        invalidate_live_sessions=invalidate_live_sessions,
        outcome_ledger=outcome_ledger,
        persist_tool_audit=persist_tool_audit,
        process_transcript=process_transcript,
        memory_service=memory_service,
        queue_reminder=queue_reminder,
        registry=registry,
        reminder_runtime=reminder_runtime,
        request_timeline_consolidation=request_timeline_consolidation,
        runtime_for_user=runtime_for_user,
        runtime_tool_declarations=runtime_tool_declarations,
        scoped_episodic_memory=scoped_episodic_memory,
        scoped_state_for_user=scoped_state_for_user,
        session_runtime_from=session_runtime_from,
        transition_case_skill=transition_case_skill,
        user_key=user_key,
        user_routing=user_routing,
        user_tools=user_tools,
        web=web,
    )
    post_activate = _taskendpoints.post_activate
    post_answer_control = _taskendpoints.post_answer_control
    post_intent_focus = _taskendpoints.post_intent_focus
    post_job_cancel = _taskendpoints.post_job_cancel
    post_reminder_create = _taskendpoints.post_reminder_create
    post_reset = _taskendpoints.post_reset
    post_result_dismiss = _taskendpoints.post_result_dismiss
    post_task_create = _taskendpoints.post_task_create
    post_task_select = _taskendpoints.post_task_select
    post_tool = _taskendpoints.post_tool
    post_voice = _taskendpoints.post_voice

    _evidenceendpoints = EvidenceEndpoints(
        apply_memory_hint=apply_memory_hint,
        artifact_port=artifact_port,
        artifacts_for_runtime=artifacts_for_runtime,
        case_owner=case_owner,
        episodic_for_user=episodic_for_user,
        get_state=get_state,
        human_help_port=human_help_port,
        identity=identity,
        outcome_ledger=outcome_ledger,
        product_database=product_database,
        request_timeline_consolidation=request_timeline_consolidation,
        runtime_checkpoint=runtime_checkpoint,
        runtime_for_user=runtime_for_user,
        scoped_episodic_memory=scoped_episodic_memory,
        scoped_state_for_user=scoped_state_for_user,
        user_key=user_key,
        validate_external_observation=validate_external_observation,
        web=web,
    )
    get_artifact_content = _evidenceendpoints.get_artifact_content
    get_artifacts = _evidenceendpoints.get_artifacts
    get_background_tasks = _evidenceendpoints.get_background_tasks
    get_prompt_audit = _evidenceendpoints.get_prompt_audit
    get_scene_media = _evidenceendpoints.get_scene_media
    post_human_help_preview = _evidenceendpoints.post_human_help_preview
    post_human_help_submit = _evidenceendpoints.post_human_help_submit
    post_observation = _evidenceendpoints.post_observation
    post_replay_observation = _evidenceendpoints.post_replay_observation
    post_scene_identity = _evidenceendpoints.post_scene_identity

    _realtimeconnection = RealtimeConnection(
        LOGGER=LOGGER,
        OmniBrowserBridge=OmniBrowserBridge,
        TaskSlot=TaskSlot,
        WSMsgType=WSMsgType,
        ambient_options=ambient_options,
        ambient_watch_session=ambient_watch_session,
        answer_executor=answer_executor,
        append_memory_audit=append_memory_audit,
        apply_memory_hint=apply_memory_hint,
        artifacts_for_runtime=artifacts_for_runtime,
        capability_overrides=capability_overrides,
        case_owner=case_owner,
        clock_for_session=clock_for_session,
        grounded_user_instructions=grounded_user_instructions,
        harness_layers=harness_layers,
        identity=identity,
        live_bridges=live_bridges,
        live_harnesses=live_harnesses,
        live_sockets=live_sockets,
        live_sockets_by_session=live_sockets_by_session,
        live_sockets_by_user=live_sockets_by_user,
        livekit_settings=livekit_settings,
        livekit_transport=livekit_transport,
        memory_recovery=memory_recovery,
        model=model,
        perception_context_for_runtime=perception_context_for_runtime,
        persist_tool_audit=persist_tool_audit,
        process_transcript=process_transcript,
        memory_service=memory_service,
        product_database=product_database,
        queue_reminder=queue_reminder,
        refresh_harness_context=refresh_harness_context,
        registry=registry,
        remember_tool_effect=remember_tool_effect,
        request_bridge_response=request_bridge_response,
        request_memory_speech=request_memory_speech,
        request_timeline_consolidation=request_timeline_consolidation,
        runtime_checkpoint=runtime_checkpoint,
        runtime_tool_declarations=runtime_tool_declarations,
        scoped_episodic_memory=scoped_episodic_memory,
        scoped_state_for_user=scoped_state_for_user,
        state=state,
        stop_case_connection=stop_case_connection,
        submit_tool_confirmation=submit_tool_confirmation,
        timeline=timeline,
        trace_event=trace_event,
        turn_policy=turn_policy,
        user_key=user_key,
        voice=voice,
        web=web,
    )
    realtime = _realtimeconnection.realtime

    # Personal diaries have their own archive and worker; existing runtime lanes are read-only inputs.
    if os.getenv("EGOASSIST_DIARY_ENABLED", "1") != "0":
        from egoassist.diary.api import mount as mount_diary
        diary_service = mount_diary(app, users_root=user_memory_root, timeline_path=timeline.path,
                    api_key=insight_generator.api_key, user_key=user_key,
                    blocked=lambda uid: uid in deleting_insight_users)
        retention.derived_stores = (diary_service,)
        _stateprojection.diary_status = diary_service.status

    from egoassist.application.history_api import mount as mount_history
    from egoassist.realtime.decisions_router import decisions_http_context
    app.cleanup_ctx.append(decisions_http_context)
    app.cleanup_ctx.append(app_memory_reads_context)
    mount_history(app, identity=identity, user_key=user_key,
                  journal_for=lambda uid, sid: SessionJournal.open_in(user_memory_root / uid / 'sessions', sid),
                  evidence=evidence_service, answers=answer_executor.answer_service)
    retention.derived_stores += (evidence_service,)

    app.add_routes(
        [
            web.post("/api/auth/register", post_register),
            web.post("/api/auth/login", post_login),
            web.post("/api/auth/logout", post_logout),
            web.get("/api/auth/me", get_me),
            web.get("/api/sessions", get_sessions),
            web.get("/api/privacy", get_privacy),
            web.get("/api/privacy/export", get_privacy_export),
            web.post("/api/privacy/retention", post_privacy_retention),
            web.post("/api/privacy/delete", post_privacy_delete),
            web.post("/api/ambient/consent", post_ambient_consent),
            web.post("/api/ambient/settings", post_ambient_settings),
            web.post("/api/interaction/resume-speech", post_resume_speech),
            web.post("/api/sessions", post_session),
            web.post("/api/sessions/memory-policy", post_memory_policy),
            web.get("/api/state", get_state),
            web.get("/api/prompts", get_prompt_audit),
            web.get("/api/conversation", _evidenceendpoints.get_conversation_history),
            web.post("/api/memory/recovery/retry", post_memory_recovery_retry),
            web.get("/api/scene/media", get_scene_media),
            web.post("/api/scene/identity", post_scene_identity),
            web.post("/api/scene/speakers/merge", _evidenceendpoints.post_speaker_merge),
            web.post("/api/scene/events", _evidenceendpoints.post_long_events),
            web.get("/api/scene/events/evidence", _evidenceendpoints.get_event_evidence),
            web.post("/api/answers/control", post_answer_control),
            web.get("/api/insights", get_insights),
            web.post("/api/insights", post_insight),
            web.post("/api/insights/policy", post_insight_policy),
            web.post("/api/insights/cancel", post_insight_cancel),
            web.get(
                "/api/memory/state", get_memory_state
            ),  # Phase 1: memory observability
            web.get(
                "/api/memory/timeline", get_timeline_memory
            ),  # unified TimelineMemory view
            web.get(
                "/api/background-tasks", get_background_tasks
            ),  # Phase 2A: task queue
            web.get("/api/artifacts", get_artifacts),
            web.get("/api/artifacts/{artifact_id}/content", get_artifact_content),
            web.post("/api/human-help/preview", post_human_help_preview),
            web.post("/api/human-help/submit", post_human_help_submit),
            web.post("/api/skills/activate", post_activate),
            web.post("/api/tasks/select", post_task_select),
            web.post("/api/tasks/create", post_task_create),
            web.post("/api/voice", post_voice),
            web.post("/api/task/observe", post_observation),
            web.post("/api/replay/observe", post_replay_observation),
            web.post("/api/task/reset", post_reset),
            web.post("/api/intents/focus", post_intent_focus),
            web.post("/api/jobs/cancel", post_job_cancel),
            web.post("/api/reminders", post_reminder_create),
            web.post("/api/livekit/token", post_livekit_token),
            web.post("/api/results/dismiss", post_result_dismiss),
            web.post("/api/candidate", post_candidate),
            web.post("/api/memory/delete", post_delete),
            web.post("/api/tools/execute", post_tool),
            web.get("/ws/realtime", realtime),
            web.get("/", get_index),
            web.static("/", WEB_ROOT),
        ]
    )
    return app
