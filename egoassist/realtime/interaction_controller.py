"""Apply validated interaction decisions to answer work and media delivery."""
from __future__ import annotations

import json

from egoassist.product.answer_jobs import answer_jobs
from egoassist.prompts import prompt_text
from .work_ledger import CaseWorkLedger


def is_output_control(semantic):
    """Only semantic output controls can bypass ordered task/effect commits."""
    return ((semantic.get("interaction") or {}).get("action") in {"listen", "pause", "cancel", "uncertain", "keep", "wait"}
            and not any(semantic.get(key) for key in
                        ("intents", "memoryAtoms", "memoryQuerySpec", "memoryDraft", "revision", "taskControl", "skillControl")))


def interaction_control(semantic):
    control = semantic.get("interaction")
    if control is None:
        control = {"audience": "assistant", "action": "listen" if semantic.get("speechControl") else "reply",
                   "output": "silent" if semantic.get("speechControl") else "speech"}
        if semantic.get("speechControl"):
            control["quiet"] = {"mode": "set", "allowReplies": True}
    return control


def preserves_speech(control):
    return control["audience"] != "assistant" or control["action"] in {"keep", "uncertain"}


async def select_speech_floor(semantic, turn_id, turn_sequence, *, owner,
                              interaction_state, harness, answer_executor, bridge, emit):
    """Claim speech after classification, independent of input arrival and FIFO work.

    The caller holds mutation_lock. A delayed old decision cannot reclaim a newer
    floor; a newer backchannel cannot invalidate a pending substantive question.
    """
    control = interaction_control(semantic)
    if preserves_speech(control) or turn_sequence <= owner.speech_floor_sequence:
        return False
    owner.speech_floor_sequence = turn_sequence
    owner.output_generation = max(owner.output_generation + 1, turn_sequence)
    ledger = CaseWorkLedger(harness.journal)
    turns = ledger.turns()
    from egoassist.core.events import latest
    for delivery in latest(harness.journal, "turn_delivery", "turnId").values():
        previous_id = delivery["turnId"]
        if (int(turns.get(previous_id, {}).get("sequence") or 0) < turn_sequence
                and delivery.get("status") in {"received", "responding", "generated"}):
            ledger.set_turn_delivery(previous_id, "interrupted", reason="semantic-floor-change")
            answer_executor.interrupt(harness.journal, previous_id)
    ledger.suspend_before_sequence(turn_sequence, reason="semantic-floor-change")
    bridge.cancel_response(drop_queued_below=101, reason="semantic-new-turn")
    # Stop the device cache too: provider generation can already be complete.
    # Apply permissions AFTER this event's synchronous media pause.
    await emit({"type": "playback_stop", "trigger": "semantic-floor-change",
                "turnId": turn_id, "generation": owner.output_generation})
    interaction_state.apply(control, turn_id, timestamp=turns[turn_id]["timestamp"])
    await emit({"type": "speech_floor", "turnId": turn_id,
                "generation": owner.output_generation, "action": control["action"]})
    harness.journal.append("interaction.floor", {"turnId": turn_id,
        "sequence": turn_sequence, "generation": owner.output_generation, "interaction": control})
    return True


async def apply_interaction(semantic, turn_id, turn_sequence, *, owner, interaction_state,
                            harness, answer_executor, user_id, bridge, ws, emit,
                            muted_response_ids, resumed_response_ids, request_bridge_response):
    control = interaction_control(semantic)
    if preserves_speech(control):
        if turn_sequence == owner.latest_input_sequence:
            await ws.send_json({"type": "speech_control", "turnId": turn_id, "action": "keep",
                "generation": owner.output_generation, "interaction": control,
                "permission": interaction_state.snapshot()})
        return True, control
    await select_speech_floor(semantic, turn_id, turn_sequence, owner=owner,
        interaction_state=interaction_state, harness=harness, answer_executor=answer_executor,
        bridge=bridge, emit=emit)
    current = turn_sequence == owner.speech_floor_sequence
    turns = CaseWorkLedger(harness.journal).turns()
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
            if action in {"uncertain", "wait"}:
                interaction_state.pause()
        await ws.send_json({"type": "speech_control", "turnId": turn_id,
            "generation": owner.output_generation,
            "action": "resume" if resumed else "cancel-answer" if action == "cancel" else "wait" if action == "wait" else "uncertain" if action == "uncertain" else "stop-speaking",
            "interaction": control, "permission": interaction_state.snapshot()})
    return True, control
