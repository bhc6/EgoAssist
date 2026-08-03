"""OpenTelemetry correlation helpers with no transcript content in attributes."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import uuid4

try:
    from opentelemetry import trace as _otel_trace  # type: ignore[import-untyped]
    _TRACER = _otel_trace.get_tracer("egoassist.product")
    _OTEL_AVAILABLE = True
except ImportError:
    _otel_trace = None  # type: ignore[assignment]
    _TRACER = None
    _OTEL_AVAILABLE = False

_FORBIDDEN_LABEL_PARTS = {"text", "transcript", "prompt", "arguments", "tool_args", "payload", "message"}


def correlation_ids(**values: str | None) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in values.items()
        if value and (key.endswith("_id") or key == "trace_id")
    }


def safe_metric_attributes(values: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    """Keep metrics low-cardinality and reject user content by construction."""
    result: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        normalized = str(key).casefold()
        if any(part in normalized for part in _FORBIDDEN_LABEL_PARTS):
            continue
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 80:
            result[str(key)] = value
    return result


@contextmanager
def product_span(name: str, correlation: Mapping[str, Any] | None = None) -> Iterator[Any]:
    """Create a span and attach only stable IDs, never raw user payloads.

    Falls back to a no-op context when opentelemetry is not installed.
    """
    if not _OTEL_AVAILABLE or _TRACER is None:
        yield None
        return
    ids = dict(correlation or {})
    with _TRACER.start_as_current_span(name) as span:
        for key, value in ids.items():
            if value is not None and key.endswith("_id"):
                span.set_attribute(f"egoassist.{key}", str(value))
        span.set_attribute("egoassist.operation_id", uuid4().hex[:16])
        yield span
