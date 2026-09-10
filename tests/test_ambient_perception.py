"""Ambient (task-free) observation contract tests.

The claim ADR 007 rests on is structural: ambient code cannot weaken the task
path, because it is a separate contract validated by a separate function. Two
tests here are load-bearing for that claim --
``test_task_mode_validation_is_unchanged_by_ambient_contract`` and
``test_ambient_payload_is_rejected_by_the_task_validator``.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from egoassist.product import (
    AmbientFrameContext,
    AmbientObservationHypothesis,
    PerceptionFrameContext,
)
from egoassist.realtime import (
    NONE_OBSERVED,
    AmbientWatchList,
    AmbientWatchSession,
    ambient_prompt,
    frame_prompt,
    validate_ambient_result,
    validate_perception_result,
)


def test_string_false_is_not_a_positive_observation():
    from egoassist.realtime.perception import parse_ambient_hypothesis

    result = parse_ambient_hypothesis({
        "activity_id": "none-observed", "observed": "false",
        "confidence": 0.9, "summary": "No activity",
    })
    assert result.observed is False


def test_open_json_fences_and_wrong_gate_schema():
    from egoassist.realtime.perception import parse_ambient_hypothesis
    raw = '```json\n{"summary":"Reading a book", "visible_evidence":["open book"], "uncertainties":[], "salient_change":true}\n```'
    result = parse_ambient_hypothesis(raw, open_observation=True)
    assert result.observed and result.summary == "Reading a book"
    with pytest.raises(ValueError, match="missing"):
        parse_ambient_hypothesis('{"scene_summary":"book", "speak":false}', open_observation=True)


CAPTURED = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
WATCHLIST = Path(__file__).resolve().parents[1] / "watchlists" / "medication.json"


def watchlist() -> AmbientWatchList:
    return AmbientWatchList.load(WATCHLIST)


def session(*, consent_id: str = "consent:user-1:ambient-observation") -> AmbientWatchSession:
    return AmbientWatchSession(watchlist(), consent_id=consent_id)


def context(
    watch: AmbientWatchSession,
    *,
    frame_id: str = "frame-1",
    captured_at: datetime = CAPTURED,
) -> AmbientFrameContext:
    return watch.frame_context(
        user_id="user-1",
        session_id="case-1",
        frame_id=frame_id,
        captured_at=captured_at,
    )


def hypothesis(
    *,
    observed: bool = True,
    activity_id: str = "take-medication",
    location_id: str = "kitchen",
    confidence: float = 0.8,
) -> AmbientObservationHypothesis:
    return AmbientObservationHypothesis(
        activity_id=activity_id,
        observed=observed,
        location_id=location_id,
        confidence=confidence,
        summary="A hand raises a cup near the counter",
        visible_evidence=["a cup is visible in hand"],
        hazards=[],
    )


def validate(captured, current, output, *, delay_ms: int = 500):
    return validate_ambient_result(
        captured,
        current,
        output,
        received_at=CAPTURED + timedelta(milliseconds=delay_ms),
        source="camera/test-model",
    )


def test_absence_is_accepted_as_positive_evidence():
    watch = session()
    value = context(watch)
    result = validate(
        value,
        value,
        hypothesis(observed=False, activity_id=NONE_OBSERVED, location_id="unknown"),
    )
    assert result.accepted
    assert result.reason == "accepted-ambient-evidence"
    assert result.observation.evidence["observed"] is False
    assert result.observation.checkpoint_id == NONE_OBSERVED
    # Namespaced so ambient rows never collide with, or masquerade as, task rows.
    assert result.observation.task_id == "ambient:medication-adherence"


def test_observed_sighting_records_activity_and_place():
    watch = session()
    value = context(watch)
    result = validate(value, value, hypothesis())
    assert result.accepted
    assert result.observation.evidence["activityId"] == "take-medication"
    assert result.observation.evidence["locationId"] == "kitchen"
    assert result.observation.allowed_transitions == []


def test_ambient_rejects_stale_and_future_frames():
    watch = session()
    value = context(watch)
    assert validate(value, value, hypothesis(), delay_ms=60_000).accepted
    assert validate(value, value, hypothesis(), delay_ms=-5_000).reason == "frame-captured-in-future"


def test_activity_outside_the_watchlist_is_rejected():
    watch = session()
    value = context(watch)
    result = validate(value, value, hypothesis(activity_id="taking-a-shower"))
    assert not result.accepted
    assert result.reason == "activity-outside-watchlist"


def test_unauthorized_location_is_rejected():
    """Aria community guidelines forbid recording in a bedroom, so no watchlist
    may authorize one and no model answer may name one."""
    watch = session()
    value = context(watch)
    assert "bedroom" not in {item["id"] for item in value.watch_locations}
    result = validate(value, value, hypothesis(location_id="bedroom"))
    assert not result.accepted
    assert result.reason == "location-outside-watchlist"


def test_malformed_provider_output_is_not_a_false_absence():
    """A model naming a real activity while claiming it was not observed must be
    rejected outright. Accepting it would write a row that the habit bridge reads
    as a sighting while the console shows nothing was seen."""
    watch = session()
    value = context(watch)
    result = validate(value, value, hypothesis(observed=False, activity_id="take-medication"))
    assert not result.accepted
    assert result.reason == "contradictory-absence-claim"
    inverse = validate(value, value, hypothesis(observed=True, activity_id=NONE_OBSERVED))
    assert not inverse.accepted
    assert inverse.reason == "contradictory-absence-claim"


def test_watchlist_change_invalidates_an_inflight_frame():
    watch = session()
    captured = context(watch)
    watch.reset()
    current = context(watch, frame_id="frame-2")
    result = validate(captured, current, hypothesis())
    assert not result.accepted
    assert result.reason == "ambient-context-changed"


def test_consent_revocation_invalidates_an_inflight_frame():
    """Consent sits inside the binding tuple, so a revocation cannot be raced by
    a frame already on its way to the provider."""
    captured = context(session(consent_id="consent:user-1:ambient-observation"))
    current = context(session(consent_id="consent:user-1:ambient-observation-v2"))
    current = current.model_copy(update={"context_id": captured.context_id})
    result = validate(captured, current, hypothesis())
    assert not result.accepted
    assert result.reason == "ambient-context-changed"


def test_frame_id_dedup_suppresses_a_repeated_ambient_frame():
    watch = session()
    assert watch.should_submit("frame-1") is True
    assert watch.should_submit("frame-1") is False
    assert watch.should_submit("frame-2") is True


def test_ambient_prompt_forbids_inferring_ingestion():
    value = context(session())
    prompt = ambient_prompt(value)
    assert "take-medication" in prompt
    assert "swallowing" in prompt
    # Absence must be presented as an expected answer, or the model will invent
    # a sighting to be helpful.
    assert "none-observed" in prompt
    assert frame_prompt(value) == prompt


def test_task_mode_validation_is_unchanged_by_ambient_contract():
    """Regression guard for ADR 007: the task validator still rejects an
    unrelated frame rather than treating it as observed absence."""
    from tests.test_structured_perception import (
        context as task_context,
        hypothesis as task_hypothesis,
        task_session,
        validate as task_validate,
    )

    task = task_session()
    value = task_context(task)
    result = task_validate(value, value, task_hypothesis(unrelated=True))
    assert not result.accepted
    assert result.reason == "unrelated-frame"
    assert frame_prompt(value) != ambient_prompt(context(session()))


def test_ambient_payload_is_rejected_by_the_task_validator():
    """Cross-feeding fails closed at the schema, not at a runtime branch."""
    value = context(session())
    with pytest.raises(ValidationError):
        PerceptionFrameContext.model_validate(value.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        validate_perception_result(
            value.model_dump(mode="json"),
            value.model_dump(mode="json"),
            hypothesis().model_dump(mode="json"),
            received_at=CAPTURED + timedelta(milliseconds=500),
            source="camera/test-model",
        )


def minimal_spec(**overrides) -> dict:
    spec = {
        "id": "test-watch",
        "title": "Test watch",
        "version": "1.0",
        "risk": "low",
        "activities": [{"id": "an-activity", "label": "An activity", "visible_evidence": "a visible cue"}],
        "locations": [{"id": "kitchen", "label": "Kitchen"}],
    }
    spec.update(overrides)
    return spec


def test_watchlist_checksum_pins_the_file_contents():
    first = watchlist()
    assert first.checksum.startswith("sha256:")
    assert first.checksum == watchlist().checksum
    altered = AmbientWatchList.from_dict(
        minimal_spec(activities=[{"id": "other", "label": "x", "visible_evidence": "y"}]),
    )
    assert altered.checksum != first.checksum


def test_open_observation_accepts_unlisted_actions_and_retains_time_and_uncertainty():
    from egoassist import PACKAGE_ROOT
    from egoassist.realtime.perception import parse_ambient_hypothesis
    watch = AmbientWatchSession(AmbientWatchList.load(PACKAGE_ROOT / "watchlists/open.json"), consent_id="grant")
    captured = context(watch)
    output = parse_ambient_hypothesis({"activity_id": "repairing a bicycle and reading a label", "observed": True,
        "location_id": "workshop", "summary": "A person holds a wrench next to a bicycle.",
        "visible_evidence": ["wrench beside rear wheel"], "uncertainties": ["repair completion unknown"],
        "clarification": None, "salient_change": True})
    accepted = validate(captured, captured, output)
    assert accepted.accepted
    evidence = accepted.observation.evidence
    assert evidence["capturedAt"] == CAPTURED.isoformat()
    assert evidence["uncertainties"] == output.uncertainties
    assert output.confidence == 0
    assert "no activity or location whitelist" in ambient_prompt(captured)
    wrong_mode = captured.model_copy(update={"observation_mode": "focused"})
    assert not validate(captured, wrong_mode, output).accepted
    assert not validate(captured, captured, output.model_copy(update={"visible_evidence": []})).accepted


def test_a_watchlist_may_not_shadow_the_reserved_ids():
    with pytest.raises(ValueError):
        AmbientWatchList.from_dict(
            minimal_spec(activities=[{"id": NONE_OBSERVED, "label": "x", "visible_evidence": "y"}]),
        )
    with pytest.raises(ValueError):
        AmbientWatchList.from_dict(minimal_spec(locations=[{"id": "unknown", "label": "x"}]))
