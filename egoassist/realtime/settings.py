"""Validated runtime budgets and model routes, shared with audit views."""
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
        "liveMaxAgeMs": integer("EGOASSIST_LIVE_FRAME_MAX_AGE_MS", 5000, maximum=300000),
    }


def asr_settings():
    return {
        "model": os.getenv("EGOASSIST_ASR_MODEL", "qwen3-asr-flash"),
        "timeoutSeconds": number("EGOASSIST_ASR_TIMEOUT", 10),
        "concurrency": integer("EGOASSIST_ASR_CONCURRENCY", 2, maximum=32),
    }


def tool_settings():
    return {
        "maxRounds": integer("EGOASSIST_TOOL_MAX_ROUNDS", 4, maximum=64),
        "maxCalls": integer("EGOASSIST_TOOL_MAX_CALLS", 16, maximum=256),
    }
