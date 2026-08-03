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
    "egoassist.product.privacy",
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
    "ProductDatabase", "Base", "correlation_ids", "product_span", "safe_metric_attributes",
    "ConversationTurn", "CorrelationIds", "IntentCandidate", "JobRecord",
    "ResultRecord", "NotificationRequest", "NotificationReceipt", "ArtifactDescriptor",
    "HumanHelpRequest", "HumanHelpReceipt", "PerceptionHazard", "PerceptionFrameContext",
    "PerceptionHypothesis", "ObservationEvidence", "ProviderDisclosure",
    "InteractionTransport", "WorkflowPort", "NotificationPort", "ArtifactPort",
    "HumanHelpPort", "PerceptionPort",
    "LocalArtifactAdapter", "WebhookHumanHelpAdapter",
    "PrivacyGovernance", "OutcomeLedger", "OutcomeRecord", "redact_pii", "redact_text",
    "provider_disclosures", "transport_security_summary", "encrypt_json", "decrypt_json",
]
