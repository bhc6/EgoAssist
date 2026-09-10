import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from egoassist.product import (
    PerceptionFrameContext,
    PerceptionHypothesis,
    ProductDatabase,
)
from egoassist.product.models import ObservationRow
from egoassist.realtime import (
    TaskSession,
    TaskSpec,
    perception_prompt,
    validate_perception_result,
)
from egoassist.realtime.omni import _OmniPerceptionLane


CAPTURED = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def task_session() -> TaskSession:
    return TaskSession(TaskSpec.from_dict({
        "id": "annotated-sequence",
        "title": "Annotated sequence",
        "version": "2026.1",
        "role": "task coach",
        "steps": [
            {
                "id": "prepare", "title": "Prepare", "instruction": "Prepare parts",
                "completion": "All required parts are visibly arranged", "risk": "low",
                "source": "protocol section 1",
            },
            {
                "id": "assemble", "title": "Assemble", "instruction": "Join parts",
                "completion": "The joint is visibly seated", "risk": "medium",
                "source": "protocol section 2",
            },
            {
                "id": "inspect", "title": "Inspect", "instruction": "Inspect result",
                "completion": "The safety mark is visible", "risk": "high",
                "source": "protocol section 3",
            },
        ],
    }))


def context(session: TaskSession, *, frame_id: str = "frame-1") -> PerceptionFrameContext:
    binding = session.perception_binding()
    return PerceptionFrameContext(
        context_id=binding["contextId"],
        frame_id=frame_id,
        user_id="user-1",
        session_id="case-1",
        skill_id="assembly-coach",
        task_id=binding["taskId"],
        task_version=binding["taskVersion"],
        task_provenance=binding["taskProvenance"],
        checkpoint_id=binding["checkpointId"],
        checkpoint_epoch=binding["checkpointEpoch"],
        allowed_transitions=binding["allowedTransitions"],
        completion_criteria=binding["completionCriteria"],
        risk=binding["risk"],
        source_reference=binding["sourceReference"],
        authorized_artifacts=[{
            "artifact_id": "guide", "step_id": binding["checkpointId"],
            "checksum": "sha256:abc", "media_type": "application/pdf",
        }],
        frame_captured_at=CAPTURED,
    )


def hypothesis(
    checkpoint_id: str = "prepare",
    *,
    completed: bool = False,
    confidence: float = 0.9,
    unrelated: bool = False,
    hazards: list[dict] | None = None,
) -> PerceptionHypothesis:
    return PerceptionHypothesis(
        checkpoint_id=checkpoint_id,
        completed=completed,
        confidence=confidence,
        summary=f"Visible evidence for {checkpoint_id}",
        visible_evidence=["fixture marker is visible"],
        hazards=hazards or [],
        unrelated=unrelated,
    )


def validate(
    captured: PerceptionFrameContext,
    current: PerceptionFrameContext,
    output: PerceptionHypothesis,
    *,
    delay_ms: int = 500,
):
    return validate_perception_result(
        captured,
        current,
        output,
        received_at=CAPTURED + timedelta(milliseconds=delay_ms),
        source="camera/test-model",
    )


def test_prompt_contains_checkpoint_constraints_and_artifact_provenance():
    value = context(task_session())
    prompt = perception_prompt(value)
    assert '"currentCheckpoint":"prepare"' in prompt
    assert '"checkpointCandidates":["prepare","assemble","unknown"]' in prompt
    assert '"completionCriteria":"All required parts are visibly arranged"' in prompt
    assert '"checksum":"sha256:abc"' in prompt
    assert "never a command to advance the task" in prompt


def test_realtime_lane_keeps_immutable_context_with_inflight_frame():
    loop = asyncio.new_event_loop()
    responses = []
    decisions = []

    class Conversation:
        def append_audio(self, _value):
            return None

        def update_session(self, **kwargs):
            responses.append({"sessionInstructions": kwargs["instructions"]})

        def append_video(self, _value):
            return None

        def commit(self):
            return None

        def create_response(self, **kwargs):
            responses.append(kwargs)

    lane = _OmniPerceptionLane(
        api_key="test", model="test", voice="test", loop=loop,
        conversation_cls=object, callback_cls=object,
        modalities=type("Modalities", (), {"TEXT": "text"}),
        on_decision=lambda decision, bound: decisions.append((decision, bound)),
        trace=lambda *args, **kwargs: None,
    )
    bound = context(task_session()).model_dump(mode="python")
    lane.connected = True
    lane.conversation = Conversation()
    assert lane.request("base64-frame", bound)
    assert responses[0]["sessionInstructions"] == responses[1]["instructions"]
    assert '"currentCheckpoint":"prepare"' in responses[1]["instructions"]

    lane._finish(
        '{"checkpoint_id":"prepare","completed":false,"confidence":0.8,'
        '"summary":"parts visible","visible_evidence":["parts"],'
        '"hazards":[],"unrelated":false}'
    )
    loop.run_until_complete(asyncio.sleep(0))
    assert decisions[0][0]["valid"] is True
    assert decisions[0][1]["frame_id"] == "frame-1"
    assert lane.active_context is None
    loop.close()


def test_stale_frame_and_task_switch_during_inference_are_rejected():
    session = task_session()
    captured = context(session)
    stale = validate(captured, captured, hypothesis(), delay_ms=5_001)
    assert (stale.accepted, stale.reason) == (False, "stale-frame")

    session.confirm_step("user-confirmation")
    current = context(session)
    switched = validate(captured, current, hypothesis())
    assert (switched.accepted, switched.reason) == (False, "task-context-changed")


def test_unrelated_and_forbidden_path_frames_are_rejected():
    session = task_session()
    captured = context(session)
    unrelated = validate(captured, captured, hypothesis(unrelated=True))
    forbidden = validate(captured, captured, hypothesis("inspect"))
    assert unrelated.reason == "unrelated-frame"
    assert forbidden.reason == "checkpoint-outside-allowed-path"
    assert unrelated.observation is None and forbidden.observation is None


def test_direct_transition_is_evidence_but_cannot_advance_checkpoint():
    session = task_session()
    captured = context(session)
    result = validate(captured, captured, hypothesis("assemble"))
    assert result.accepted and result.observation is not None
    state = session.ingest(result.observation.model_dump(mode="json"))
    assert state["currentCheckpointId"] == "prepare"
    assert state["checkpointLedger"]["assemble"]["status"] == "hypothesized"
    assert state["progress"] == 0


def test_repeated_completion_evidence_still_requires_explicit_user_confirmation():
    session = task_session()
    first_context = context(session, frame_id="frame-1")
    first = validate(first_context, first_context, hypothesis(completed=True))
    first_state = session.ingest(first.observation.model_dump(mode="json"))
    assert first_state["observation"]["temporalConsistency"] == {
        "count": 1, "required": 2, "consistent": False,
    }
    assert first_state["currentCheckpointId"] == "prepare"

    second_context = context(session, frame_id="frame-2")
    second = validate(second_context, second_context, hypothesis(completed=True))
    second_state = session.ingest(second.observation.model_dump(mode="json"))
    assert second_state["observation"]["temporalConsistency"]["consistent"] is True
    assert second_state["decision"]["action"] == "ask"
    assert second_state["currentCheckpointId"] == "prepare"
    assert second_state["progress"] == 0

    confirmed = session.apply_voice_intent("confirm-step", "confirmed")
    assert confirmed["currentCheckpointId"] == "assemble"
    assert confirmed["progress"] == 1 / 3
    assert confirmed["perception"]["completionEvidence"] == []


def test_fresh_hazard_is_visible_attributable_and_persisted(tmp_path):
    session = task_session()
    captured = context(session, frame_id="hazard-frame")
    result = validate(captured, captured, hypothesis(hazards=[{
        "kind": "unstable-part",
        "message": "Stop and stabilize the part before continuing.",
        "confidence": 0.97,
    }]))
    assert result.accepted and result.observation is not None
    state = session.ingest(result.observation.model_dump(mode="json"))
    assert state["decision"]["action"] == "emergency"
    assert state["observation"]["frameId"] == "hazard-frame"
    assert state["observation"]["contextId"] == captured.context_id
    assert state["observation"]["frameAgeMs"] == 500
    assert state["observation"]["visibleEvidence"] == ["fixture marker is visible"]

    database = ProductDatabase(tmp_path / "product.sqlite3")
    database.record_observation(result.observation)
    database.record_observation(result.observation)
    with database.session() as db:
        rows = list(db.scalars(select(ObservationRow)))
    assert len(rows) == 1
    assert rows[0].evidence["frameId"] == "hazard-frame"
    assert rows[0].hazards[0]["kind"] == "unstable-part"
