"""Auditable and Skill-configurable full-duplex turn-taking policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TurnTakingPolicy:
    immediate_actions: tuple[str, ...] = ("guide", "silent")
    finish_clause_actions: tuple[str, ...] = ("ask", "correct")
    protected_actions: tuple[str, ...] = ("emergency",)
    max_protection_ms: int = 2500

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> "TurnTakingPolicy":
        raw = manifest.get("turn_taking", {})
        return cls(
            immediate_actions=tuple(map(str, raw.get("immediate_actions", cls.immediate_actions))),
            finish_clause_actions=tuple(map(str, raw.get("finish_clause_actions", cls.finish_clause_actions))),
            protected_actions=tuple(map(str, raw.get("protected_actions", cls.protected_actions))),
            max_protection_ms=max(250, min(5000, int(raw.get("max_protection_ms", cls.max_protection_ms)))),
        )

    def select(self, decision: dict[str, Any]) -> dict[str, Any]:
        explicit = decision.get("speechPolicy")
        if explicit in {"immediate", "finish-clause", "protected"}:
            policy = explicit
            basis = "decision-explicit"
        else:
            action = str(decision.get("action", "guide"))
            if action in self.protected_actions:
                policy, basis = "protected", "skill-action-rule"
            elif action in self.finish_clause_actions:
                policy, basis = "finish-clause", "skill-action-rule"
            else:
                policy, basis = "immediate", "default-action-rule"
        return {
            "policy": policy,
            "basis": basis,
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "maxProtectionMs": self.max_protection_ms if policy == "protected" else 0,
        }

    def public(self) -> dict[str, Any]:
        return {
            "immediateActions": list(self.immediate_actions),
            "finishClauseActions": list(self.finish_clause_actions),
            "protectedActions": list(self.protected_actions),
            "maxProtectionMs": self.max_protection_ms,
        }

