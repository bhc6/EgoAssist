"""Compatibility exports for shared model-call auditing."""

from egoassist.core.model_audit import (
    PAIRS,
    catalogue,
    detail,
    has_output,
    page,
    record_output,
    record_request,
)

__all__ = [
    "PAIRS", "catalogue", "detail", "has_output", "page",
    "record_output", "record_request",
]
