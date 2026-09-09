"""Locale-aware prompt strings for model instructions and user-visible messages.

The active bundle is selected from the ``EGOASSIST_LANG`` environment variable
(default ``"zh-CN"``).  Supported values: ``"zh-CN"``, ``"en"``.

Usage::

    from egoassist.realtime.locale_prompts import PROMPTS

    header = PROMPTS.evidence_header
    instr  = PROMPTS.task_confirm_step.format(message=decision["message"])

Prompt content lives in ``egoassist/prompts/locale_zh.json`` and
``locale_en.json``. Restart the service after editing a locale bundle.
"""

from __future__ import annotations

from egoassist.prompts import prompt_bundle

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptBundle:
    """All locale-sensitive strings consumed by the realtime harness.

    Fields marked *template* contain ``{name}`` placeholders; call
    ``.format(name=value)`` at the use site.
    """

    # ── Evidence injection (context function) ─────────────────────────────
    evidence_header: str         # section label prepended to evidence block
    evidence_instruction: str    # guidance to the model on how to use evidence

    # ── Grounding constraints (grounded_user_instructions) ────────────────
    grounding_referential_no_evidence: str  # referential query; no evidence
    grounding_no_evidence: str              # general query; no evidence

    # ── Skill / task control (control_instructions) ───────────────────────
    task_repeat: str
    task_request_help: str
    task_confirm_step: str       # template: .format(message=…)
    task_rollback: str
    task_pause: str
    task_resume: str
    task_general: str            # suffix for unrecognised task_control values

    # ── Proactive speech ──────────────────────────────────────────────────
    decision_speech: str         # template: .format(message=…, reason=…)
    memory_speech: str           # suffix appended to preference-recall instruction

    # ── Memory save responses ─────────────────────────────────────────────
    memory_saved_prefix: str     # prefixed before joined card summaries
    memory_needs_input: str
    memory_not_committed: str

    # ── Task spec building ────────────────────────────────────────────────
    role_suffix: str             # appended to skill name as the model role
    safety_policy_domain: str    # first safety-policy list item
    safety_policy_uncertainty: str  # second safety-policy list item
    step_title: str              # template: .format(n=…)
    step_completion: str         # template: .format(instruction=…)

    # ── API error messages ────────────────────────────────────────────────
    error_internal: str

    # ── Diagnostic / decision_state payload strings ───────────────────────
    diag_evidence_waiting: str
    diag_candidate_none: str
    diag_confirmation_none: str
    diag_retrieval_waiting_scope: str
    diag_retrieval_disabled: str
    diag_impact_reused: str
    diag_impact_ready: str
    diag_impact_none: str
    diag_decision_waiting: str
    diag_decision_general: str
    diag_task_confirmed_count: str   # template: .format(count=…)
    diag_task_no_match: str
    diag_mem_working_name: str
    diag_mem_working_impl: str
    diag_mem_preference_name: str
    diag_mem_preference_impl: str
    diag_mem_episodic_name: str
    diag_mem_episodic_impl_no_task: str
    diag_mem_episodic_impl_with_task: str
    diag_mem_knowledge_name: str
    diag_mem_knowledge_impl_no_task: str
    diag_mem_knowledge_impl_with_task: str
    diag_claim_boundary: str



# ─────────────────────────────────────────────────────────────────────────────
# zh-CN bundle — preserves all current behaviour exactly
# ─────────────────────────────────────────────────────────────────────────────
_ZH_CN = PromptBundle(**prompt_bundle("locale_zh"))

# ─────────────────────────────────────────────────────────────────────────────
# en bundle — English equivalents; behaviour-compatible with zh-CN
# ─────────────────────────────────────────────────────────────────────────────
_EN = PromptBundle(**prompt_bundle("locale_en"))

# ─────────────────────────────────────────────────────────────────────────────
# Registry and factory
# ─────────────────────────────────────────────────────────────────────────────
_BUNDLES: dict[str, PromptBundle] = {
    "zh-CN": _ZH_CN,
    "zh": _ZH_CN,
    "en": _EN,
    "en-US": _EN,
    "en-GB": _EN,
}


def get_prompts(locale: str | None = None) -> PromptBundle:
    """Return the prompt bundle for *locale* (default: ``EGOASSIST_LANG`` env var).

    Falls back to ``"zh-CN"`` if the locale is unknown so existing deployments
    are unaffected by unset env vars.
    """
    resolved = (locale or os.getenv("EGOASSIST_LANG", "zh-CN")).strip()
    return _BUNDLES.get(resolved, _ZH_CN)


#: Module-level singleton — import this in call sites.
PROMPTS: PromptBundle = get_prompts()
