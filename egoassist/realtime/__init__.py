"""Observable realtime assistance, memory, Skills, and turn-taking primitives."""

from .engine import PreferenceMemory
from .episodes import RealtimeEpisodeMemory
from .perception import (
    MAX_FRAME_AGE_MS,
    PerceptionValidation,
    StructuredPerceptionAdapter,
    ambient_prompt,
    frame_prompt,
    parse_ambient_hypothesis,
    parse_perception_hypothesis,
    perception_prompt,
    validate_ambient_result,
    validate_perception_result,
)
from .ambient import (
    AMBIENT_SCHEMA_VERSION,
    NONE_OBSERVED,
    UNKNOWN_LOCATION,
    AmbientWatchItem,
    AmbientWatchList,
    AmbientWatchLocation,
    AmbientWatchSession,
)
from .task import TaskSession, TaskSpec, bounded_text
from .skills import SkillPackage, SkillRegistry, SkillRuntime
from .tools import SkillToolExecutor
from .trace import EventLedger
from .speech import TurnTakingPolicy
from .context_harness import (
    ContextHarness, ContextLayer, ContextSettings, SessionJournal, estimate_tokens,
)
from .capabilities import (
    executable_tool_declarations,
    platform_capability_available,
    platform_capability_truth,
    public_tool_declaration,
    skill_capability_truth,
    tool_truth,
)
from .identity import IdentityStore
from .memory_policy import (
    MEMORY_POLICY_VERSION, default_memory_policy, memory_profiles, normalize_memory_policy,
)
from .semantic import (
    SemanticTurnRouter, TurnAccumulator, TURN_ROUTER_POLICY_VERSION, memory_draft_candidate,
    is_fast_turn_control,
)
from .locale_prompts import PROMPTS, PromptBundle, get_prompts
from .work_ledger import CaseWorkLedger, ExecutionEnvelope
try:
    from .livekit_transport import (
        LIVEKIT_DATA_TOPIC,
        LiveKitInteractionTransport,
        LiveKitSettings,
        livekit_capability,
    )
except ImportError:  # livekit is an optional dependency
    LIVEKIT_DATA_TOPIC = None  # type: ignore[assignment]
    LiveKitInteractionTransport = None  # type: ignore[assignment,misc]
    LiveKitSettings = None  # type: ignore[assignment,misc]

    def livekit_capability(*_args, **_kwargs) -> dict:  # type: ignore[misc]
        return {"available": False, "reason": "LiveKit RTC/API dependencies are not installed."}
from .intent_decomposition import normalize_intent_candidates

__all__ = [
    "PreferenceMemory", "RealtimeEpisodeMemory", "StructuredPerceptionAdapter",
    "MAX_FRAME_AGE_MS", "PerceptionValidation", "parse_perception_hypothesis",
    "perception_prompt", "validate_perception_result",
    "AMBIENT_SCHEMA_VERSION", "NONE_OBSERVED", "UNKNOWN_LOCATION",
    "AmbientWatchItem", "AmbientWatchList", "AmbientWatchLocation", "AmbientWatchSession",
    "ambient_prompt", "frame_prompt", "parse_ambient_hypothesis", "validate_ambient_result",
    "TaskSession", "TaskSpec", "bounded_text", "SkillPackage", "SkillRegistry",
    "SkillRuntime", "SkillToolExecutor", "EventLedger", "TurnTakingPolicy",
    "ContextHarness", "ContextLayer", "ContextSettings", "SessionJournal", "estimate_tokens",
    "IdentityStore",
    "MEMORY_POLICY_VERSION", "default_memory_policy", "memory_profiles", "normalize_memory_policy",
    "SemanticTurnRouter", "TurnAccumulator", "TURN_ROUTER_POLICY_VERSION", "memory_draft_candidate",
    "is_fast_turn_control",
    "CaseWorkLedger", "ExecutionEnvelope",
    "PROMPTS", "PromptBundle", "get_prompts",
    "LIVEKIT_DATA_TOPIC",
    "LiveKitInteractionTransport",
    "LiveKitSettings",
    "livekit_capability",
    "normalize_intent_candidates",
    "executable_tool_declarations", "platform_capability_available",
    "platform_capability_truth", "public_tool_declaration",
    "skill_capability_truth", "tool_truth",
]
