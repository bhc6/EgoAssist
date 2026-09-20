"""Apply validated interaction decisions to answer work and media delivery."""
from __future__ import annotations

import json

from egoassist.product.answer_jobs import answer_jobs
from egoassist.prompts import prompt_text
from .work_ledger import CaseWorkLedger


def is_output_control(semantic):
    """Only semantic output controls can bypass ordered task/effect commits."""
    return ((semantic.get("interaction") or {}).get("action") in {"listen", "pause", "cancel", "uncertain"}
            and not any(semantic.get(key) for key in
                        ("intents", "memoryAtoms", "memoryQuerySpec", "memoryDraft", "revision", "taskControl", "skillControl")))


async def apply_interaction(semantic, turn_id, turn_sequence, *, owner, interaction_state,
                            harness, answer_executor, user_id, bridge, ws, emit,
                            muted_response_ids, resumed_response_ids, request_bridge_response):
    control = semantic.get("interaction")
    if control is None:
        control = {"audience": "assistant", "action": "listen" if semantic.get("speechControl") else "reply",
                   "output": "silent" if semantic.get("speechControl") else "speech"}
        if semantic.get("speechControl"):
            control["quiet"] = {"mode": "set", "allowReplies": True}
    current = turn_sequence == owner.latest_input_sequence
    turns = CaseWorkLedger(harness.journal).turns()
    if current:
        interaction_state.apply(control, turn_id,
            timestamp=turns[turn_id]["timestamp"])
    target = control.get("targetTurnId")
    action = control["action"]
    answers = answer_jobs(harness.journal)
    if target and target in answers:
        if action == "cancel":
            answer_executor.cancel(harness.journal, target, reason="explicit-answer-cancellation")
        elif action == "pause":
            answer_executor.pause(harness.journal, target)
    if action == "resume" and not target:
        target = (interaction_state.playback_context() or {}).get("turnId")
        if not target:
            target = next((key for key, row in reversed(list(answers.items())) if row["status"] != "cancelled"), None)
    if action == "resume" and target in answers and control["output"] == "text":
        answer_executor.resume(user_id, harness.journal, target)
        answer_executor.interrupt(harness.journal, target)
    has_work = bool(semantic.get("intents") or semantic.get("memoryAtoms") or semantic.get("memoryQuerySpec") or semantic.get("memoryDraft"))
    if action == "reply" or (has_work and control["audience"] == "assistant" and action not in {"uncertain", "resume"}):
        if current:
            bridge.cancel_response(drop_queued_below=101, reason="semantic-new-turn")
        return False, control
    if current:
        resumed = False
        resume_target = target if action == "resume" else (
            (interaction_state.playback_context() or {}).get("turnId") if control["audience"] == "other" else None)
        can_resume = resume_target in answers and answers[resume_target]["status"] != "cancelled"
        permission = interaction_state.snapshot()
        if can_resume and (control["output"] == "speech" or control["audience"] == "other") and (
            not permission["quiet"] or (control["audience"] == "assistant" and permission["allowReplies"])):
            interaction_state.grants.add(turn_id)
            resumed = True
            tail = interaction_state.tail(resume_target)
            if tail and tail[2]:
                response_id, row, pcm = tail
                muted_response_ids.discard(response_id)
                resumed_response_ids[response_id] = {"turnId": turn_id, "answerObligation": resume_target,
                                                      "generation": owner.output_generation}
                await ws.send_json({"type": "playback_resume", "turnId": turn_id, "responseId": response_id,
                                    "offsetSamples": row["playedSamples"], "targetTurnId": resume_target})
                import base64
                for offset in range(0, len(pcm), 24000):
                    await emit({"type": "audio", "audio": base64.b64encode(pcm[offset:offset + 24000]).decode(),
                                "responseId": response_id, "purpose": "conversation", "replay": True,
                                "origin": {"turnId": turn_id, "generation": owner.output_generation}})
                harness.journal.append("interaction.resume", {"turnId": turn_id, "targetTurnId": resume_target,
                    "responseId": response_id, "offsetSamples": row["playedSamples"], "method": "cached-audio"})
            else:
                bridge.cancel_response(drop_queued_below=101, reason="resume-without-audio-cache")
                answer_executor.resume(user_id, harness.journal, resume_target)
                original = answers[resume_target]
                text_input = any(turns.get(key, {}).get("source") == "explicit-control"
                                 for key in (turn_id, resume_target))
                request_bridge_response(bridge, prompt_text("interaction/resume") + "\n" + json.dumps({
                    "question": original["question"], "context": original.get("context"),
                    "playback": interaction_state.playback_context()}, ensure_ascii=False),
                    priority=100, reason="resume-original-answer", origin={"turnId": turn_id,
                    "answerObligation": resume_target, "generation": owner.output_generation,
                    "inputChannel": "explicit-control" if text_input else "microphone"})
        else:
            bridge.cancel_response(drop_queued_below=101, reason="semantic-stop-speaking" if semantic.get("speechControl") else "semantic-interaction-control")
            if action == "uncertain":
                interaction_state.pause()
        await ws.send_json({"type": "speech_control", "turnId": turn_id,
            "action": "resume" if resumed else "cancel-answer" if action == "cancel" else "uncertain" if action == "uncertain" else "stop-speaking",
            "interaction": control, "permission": interaction_state.snapshot()})
    return True, control
