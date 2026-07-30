"""Lazy product exports keep Temporal workflow imports deterministic and small."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_MODULES = (
    "egoassist.product.contracts",
    "egoassist.product.ports",
    "egoassist.product.database",
    "egoassist.product.models",
    "egoassist.product.telemetry",
    "egoassist.product.artifacts",
    "egoassist.product.human_help",
)


def __getattr__(name: str) -> Any:
    for module_name in _MODULES:
        module = import_module(module_name)
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(name)


__all__ = [
    "ProductDatabase", "Base", "correlation_ids", "current_trace_id", "product_span",
    "ConversationTurn", "CorrelationIds", "IntentCandidate", "IntentEnvelope", "JobRecord", "JobAttempt",
    "ResultRecord", "NotificationRequest", "NotificationReceipt", "ArtifactDescriptor",
    "HumanHelpRequest", "HumanHelpReceipt", "ObservationEvidence", "ProviderDisclosure",
    "InteractionTransport", "WorkflowPort", "NotificationPort", "ArtifactPort",
    "HumanHelpPort", "PerceptionPort",
    "LocalArtifactPort", "WebhookHumanHelpPort",
]
