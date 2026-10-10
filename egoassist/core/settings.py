"""Validated shared runtime budgets and model routes."""

import math
import os


def number(name, default, *, minimum=0.1, maximum=300):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def integer(name, default, *, minimum=1, maximum=65536):
    value = number(name, default, minimum=minimum, maximum=maximum)
    if not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(value)


def perception_settings():
    return {
        "requestTimeoutSeconds": number("EGOASSIST_PERCEPTION_TIMEOUT", 8),
        "liveMaxAgeMs": integer(
            "EGOASSIST_LIVE_FRAME_MAX_AGE_MS", 5000, maximum=300000
        ),
        "requestedReminderMaxAgeMs": integer(
            "EGOASSIST_REQUESTED_REMINDER_MAX_AGE_MS", 20000, maximum=60000
        ),
    }


def observation_speech_max_age_ms(notification=None):
    settings = perception_settings()
    return settings['requestedReminderMaxAgeMs'] if isinstance(notification, dict) and notification.get('kind') == 'requested-reminder' else settings['liveMaxAgeMs']


def asr_settings():
    return {
        "model": os.getenv("EGOASSIST_ASR_MODEL", "qwen3-asr-flash"),
        "timeoutSeconds": number("EGOASSIST_ASR_TIMEOUT", 10),
        "concurrency": integer("EGOASSIST_ASR_CONCURRENCY", 2, maximum=32),
    }


def tool_settings():
    return {
        "enabled": tool_calls_enabled(),
        "maxRounds": integer("EGOASSIST_TOOL_MAX_ROUNDS", 4, maximum=64),
        "maxCalls": integer("EGOASSIST_TOOL_MAX_CALLS", 16, maximum=256),
    }


def tool_calls_enabled():
    return bool(integer("EGOASSIST_TOOL_CALLS_ENABLED", 0, minimum=0, maximum=1))


def option(name, default, *, allowed):
    """Read a validated string option from the environment.

    Used for behaviour switches whose default must stay on the conservative
    branch so that existing deployments and regression suites are unaffected
    until a run explicitly opts in.
    """
    value = str(os.getenv(name, default)).strip().casefold()
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")
    return value


# How strictly an unresolved reference (a pronoun or deictic with no extracted
# subject) blocks recall at query time.
#   strict   - such cards are never eligible. Historical default.
#   demote   - they stay eligible but always rank below source-scoped evidence.
#   off      - no reference-based ranking pressure at all.
REFERENCE_POLICIES = {"strict", "demote", "off"}
DEFAULT_REFERENCE_POLICY = "strict"


def reference_policy():
    return option(
        "EGOASSIST_MEMORY_REFERENCE_POLICY",
        DEFAULT_REFERENCE_POLICY,
        allowed=REFERENCE_POLICIES,
    )
