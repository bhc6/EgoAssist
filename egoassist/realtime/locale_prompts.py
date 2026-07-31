"""Locale-aware prompt strings for model instructions and user-visible messages.

The active bundle is selected from the ``EGOASSIST_LANG`` environment variable
(default ``"zh-CN"``).  Supported values: ``"zh-CN"``, ``"en"``.

Usage::

    from egoassist.realtime.locale_prompts import PROMPTS

    header = PROMPTS.evidence_header
    instr  = PROMPTS.task_confirm_step.format(message=decision["message"])

Adding a new locale: define a new ``PromptBundle(...)`` constant at the bottom
of this file and register it in ``_BUNDLES``.
"""

from __future__ import annotations

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
    skill_unresolved: str        # skill loaded but task not yet confirmed
    no_skill: str                # no domain skill active
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
_ZH_CN = PromptBundle(
    evidence_header="\n[本轮情景记忆证据]",
    evidence_instruction=(
        "以下是从持久化文本摘要中检索出的候选证据，不是隐藏思维。"
        "仅在与本轮问题相关时使用；证据不足时明确说不确定。"
    ),
    grounding_referential_no_evidence=(
        " 这是一个指代既往内容的问题，但记忆库中没有检索到可靠证据。"
        " 【硬性约束】禁止从通用知识推测或捏造具体数值（电话号码、地址、日期、金额等）；"
        "必须直接告知用户该信息未找到，请其确认或补充。"
    ),
    grounding_no_evidence=(
        " 记忆库中没有与本问题直接相关的证据。"
        " 【硬性约束】禁止凭通用知识推测具体数值（如号码、地址、时间、金额）；"
        "如需引用具体事实，必须有证据支撑；无证据时明确说明不确定。"
    ),
    skill_unresolved="自然回应并只询问一个确定目标所必需的问题；不要套用默认任务。",
    no_skill="当前没有领域 Skill；不要虚构任务、步骤或内部决策。",
    task_repeat=(
        "用户请求重复上一条实际说出的指导。结合当前画面和当前任务状态，用更短、更清楚的一句话重述；"
        "不要退回照读步骤文本，不要假设步骤已完成。"
    ),
    task_request_help=(
        "用户现在需要帮助。结合当前画面、当前步骤、已确认进度和安全证据，只给一个现在可执行的动作；"
        "若画面不足以判断，先问一个最关键的澄清问题。"
    ),
    task_confirm_step=(
        "用户已明确确认上一步完成。简短确认，然后依据当前画面给当前新步骤的一个可执行动作。"
        "结构化控制面当前为：{message}"
    ),
    task_rollback="用户要求回到上一个任务 checkpoint。简短确认，只说当前恢复步骤的一个动作。",
    task_pause="用户要求暂停。只简短确认已暂停，不再给下一步动作。",
    task_resume="用户恢复任务。结合当前画面和当前步骤，只给一个现在可执行的动作。",
    task_general=(
        "结合当前画面与任务状态自然应答；"
        "不要假设不可见事实，也不要一次给多个动作。"
    ),
    decision_speech=(
        "结构化感知或控制面刚产生了需要发声的决策：{message}；依据：{reason}。"
        "结合当前画面核对后自然地说出必要内容。风险必须立即明确提示；非风险只给一个动作或一个问题。"
    ),
    memory_speech="结合当前画面自然地给出一句简短语音提醒；不要提及数据库、记忆编号或内部步骤。",
    memory_saved_prefix="已保存：",
    memory_needs_input="还没有保存，请把要记住的完整内容告诉我。",
    memory_not_committed="这条内容没有保存成功，请再说一遍。",
    role_suffix="实时辅助助手",
    safety_policy_domain="遵守当前 Skill 的领域安全边界",
    safety_policy_uncertainty="不确定时询问，不根据单帧自动推进",
    step_title="步骤 {n}",
    step_completion="用户确认已完成：{instruction}",
    error_internal="请求处理失败，请查看服务端日志。",
    diag_evidence_waiting="等待实时对话或感知证据",
    diag_candidate_none="没有待确认候选",
    diag_confirmation_none="尚无活动任务范围",
    diag_retrieval_waiting_scope="确定任务后才执行精确偏好召回",
    diag_retrieval_disabled="本案例已关闭确认偏好记忆",
    diag_impact_reused="已注入本会话，后续避免重复打扰",
    diag_impact_ready="等待实时事件触发语音提醒",
    diag_impact_none="不改变当前决策",
    diag_decision_waiting="等待确认具体目标",
    diag_decision_general="通用观察与对话",
    diag_task_confirmed_count="当前任务有 {count} 条用户确认记忆",
    diag_task_no_match="当前情境没有匹配记忆",
    diag_mem_working_name="工作记忆",
    diag_mem_working_impl="Realtime 会话上下文与最近结构化事件",
    diag_mem_preference_name="参与者时间线",
    diag_mem_preference_impl="显式长期卡片 + 待确认推断偏好 + 精确任务触发",
    diag_mem_episodic_name="案例时间线",
    diag_mem_episodic_impl_no_task="current/short/long 文本卡片；不保存原始媒体",
    diag_mem_episodic_impl_with_task="current/short/long 文本卡片；视觉生产器离线适配",
    diag_mem_knowledge_name="知识记忆",
    diag_mem_knowledge_impl_no_task="领域 Skill 尚未加载",
    diag_mem_knowledge_impl_with_task="Skill/Task 静态上下文，尚无运行时语义检索",
    diag_claim_boundary="不是语义知识检索；references/ 当前未被运行时查询",
)

# ─────────────────────────────────────────────────────────────────────────────
# en bundle — English equivalents; behaviour-compatible with zh-CN
# ─────────────────────────────────────────────────────────────────────────────
_EN = PromptBundle(
    evidence_header="\n[Retrieved Episodic Evidence]",
    evidence_instruction=(
        "The following are candidate evidence items retrieved from persistent text summaries."
        " Use only what is relevant to the current question; state uncertainty when evidence is insufficient."
    ),
    grounding_referential_no_evidence=(
        " This question refers to prior content, but no reliable evidence was found in the memory store."
        " [Hard constraint] Do not infer or fabricate specific values (phone numbers, addresses, dates, amounts, etc.)"
        " from general knowledge; tell the user directly that the information was not found and ask them to confirm or provide it."
    ),
    grounding_no_evidence=(
        " No evidence directly relevant to this question was found in the memory store."
        " [Hard constraint] Do not infer specific values (numbers, addresses, times, amounts) from general knowledge;"
        " any specific fact cited must be evidence-backed; when no evidence exists, say so explicitly."
    ),
    skill_unresolved="Respond naturally and ask only the single question needed to confirm the goal; do not assume a default task.",
    no_skill="No domain Skill is active; do not fabricate tasks, steps, or internal decisions.",
    task_repeat=(
        "The user asked to repeat the last instruction given. Rephrase it in one shorter, clearer sentence"
        " using the current view and task state; do not read back step text verbatim or assume the step is done."
    ),
    task_request_help=(
        "The user needs help right now. Based on the current view, current step, confirmed progress, and safety evidence,"
        " give exactly one action that can be taken now;"
        " if the view is insufficient to judge, ask the single most important clarifying question first."
    ),
    task_confirm_step=(
        "The user has explicitly confirmed the last step is done. Briefly acknowledge, then give one actionable"
        " instruction for the current new step based on the current view."
        " Structured control panel is now: {message}"
    ),
    task_rollback="The user wants to roll back to the previous task checkpoint. Briefly confirm and give only the action for the restored step.",
    task_pause="The user asked to pause. Briefly confirm the pause only; do not give a next action.",
    task_resume="The user resumed the task. Based on the current view and current step, give exactly one action that can be taken now.",
    task_general=(
        "Respond naturally to the current view and task state;"
        " do not assume unseen facts and do not give more than one action at a time."
    ),
    decision_speech=(
        "The structured perception panel just produced a decision requiring speech: {message}; reason: {reason}."
        " Check against the current view, then state only what is necessary."
        " Risks must be flagged immediately; non-risks get one action or one question."
    ),
    memory_speech="Give one brief spoken reminder based on the current view; do not mention database IDs, memory numbers, or internal steps.",
    memory_saved_prefix="Saved: ",
    memory_needs_input="Nothing saved yet — please tell me the full content you want remembered.",
    memory_not_committed="This item was not saved — please say it again.",
    role_suffix="real-time assistant",
    safety_policy_domain="Stay within the domain safety boundaries of the current Skill",
    safety_policy_uncertainty="Ask when uncertain; do not advance automatically from a single frame",
    step_title="Step {n}",
    step_completion="User confirmed complete: {instruction}",
    error_internal="Request processing failed; check server logs.",
    diag_evidence_waiting="waiting for realtime dialogue or perception evidence",
    diag_candidate_none="no pending candidate",
    diag_confirmation_none="no active task scope",
    diag_retrieval_waiting_scope="precise preference recall runs after task is confirmed",
    diag_retrieval_disabled="preference memory disabled for this case",
    diag_impact_reused="injected this session — avoid repeating",
    diag_impact_ready="waiting for realtime event to trigger spoken reminder",
    diag_impact_none="no change to current decision",
    diag_decision_waiting="waiting to confirm specific goal",
    diag_decision_general="general observation and dialogue",
    diag_task_confirmed_count="current task has {count} user-confirmed memory item(s)",
    diag_task_no_match="no matching memory for current context",
    diag_mem_working_name="working memory",
    diag_mem_working_impl="Realtime session context and recent structured events",
    diag_mem_preference_name="participant timeline",
    diag_mem_preference_impl="explicit long-term cards + pending inferred preferences + precise task triggers",
    diag_mem_episodic_name="case timeline",
    diag_mem_episodic_impl_no_task="current/short/long text cards; raw media not stored",
    diag_mem_episodic_impl_with_task="current/short/long text cards; visual producer offline adapter",
    diag_mem_knowledge_name="knowledge memory",
    diag_mem_knowledge_impl_no_task="domain Skill not yet loaded",
    diag_mem_knowledge_impl_with_task="Skill/Task static context; no runtime semantic retrieval yet",
    diag_claim_boundary="not semantic knowledge retrieval; references/ not queried at runtime",
)

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
