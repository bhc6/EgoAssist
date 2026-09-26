from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from egoassist.memory import TimelineMemory
from egoassist.product.contracts import AmbientObservationHypothesis
from egoassist.realtime.context_harness import SessionJournal
from egoassist.realtime.observation_reviews import ObservationReviews
from egoassist.realtime.scene import SharedScene


def fixture(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    reviews = ObservationReviews(journal)
    at = datetime.now(timezone.utc)
    context = SimpleNamespace(frame_captured_at=at, media={"sha256": "new"}, reference_context={
        "userMessages": [{"sourceId": "original", "text": "table is blue", "recordedAt": (at-timedelta(seconds=2)).isoformat()}],
        "memory": [{"id": "derived", "text": "table is blue", "sourceIds": ["original"], "recordedAt": (at-timedelta(seconds=1)).isoformat()}]})
    hypothesis = AmbientObservationHypothesis(activity_id="table", summary="red table", observed=True,
        visible_evidence=["red cloth"], clarification="Has the cloth changed?",
        reference_check={"source_ids": ["original", "derived"], "evidence_indices": [0], "relation": "conflict", "reason": "different colors"})
    return journal, reviews, context, hypothesis


def consider(reviews, context, hypothesis, **kwargs):
    observation = reviews.journal.append("ambient_observation", {"observation_id": "observation-"+str(len(reviews.rows)),
        "status": "accepted", "evidence": {"capturedAt": context.frame_captured_at.isoformat(), "visibleEvidence": hypothesis.visible_evidence}})
    return reviews.consider(context, hypothesis, observation,
        **{"fresh": True, "enabled": True, "busy": False, "allowed": True, "pending": None, "cooldown": 90, **kwargs})


def test_shared_lineage_is_not_counted_twice_and_review_survives_question_capture(tmp_path):
    journal, reviews, context, hypothesis = fixture(tmp_path)
    row = consider(reviews, context, hypothesis)
    assert row["sourceRoots"] == ["original"]
    assert row["referenceStatus"] == "source-bound-hypothesis"
    assert row["claim"] == "model-comparison-not-confirmed-fact"
    scene = SharedScene(journal, "")
    turn = scene.capture("question", explicit=True)
    assert turn["observationContext"]["referenceReview"]["sourceRoots"] == ["original"]


@pytest.mark.parametrize("error", ["unknown-reference", "unknown-visible-evidence", "reference-recorded-after-frame", "same-frame-is-not-independent-evidence"])
def test_invalid_comparisons_do_not_authorize_questions(tmp_path, error):
    _, reviews, context, hypothesis = fixture(tmp_path)
    if error == "unknown-reference":
        hypothesis.reference_check["source_ids"] = ["invented"]
    elif error == "unknown-visible-evidence":
        hypothesis.reference_check["evidence_indices"] = [9]
    elif error == "reference-recorded-after-frame":
        context.reference_context["memory"][0]["recordedAt"] = (context.frame_captured_at+timedelta(seconds=1)).isoformat()
    else:
        context.reference_context["memory"][0]["frame"] = context.media
    row = consider(reviews, context, hypothesis)
    assert row["status"] == "suppressed" and row["referenceStatus"] == error


def test_deferred_question_reconsidered_on_fresh_frame_without_scene_change(tmp_path):
    _, reviews, context, hypothesis = fixture(tmp_path)
    first = consider(reviews, context, hypothesis, busy=True)
    assert first["status"] == "deferred"
    hypothesis.follow_up_of = first["candidateId"]
    hypothesis.salient_change = False
    context.reference_context["questionHistory"] = reviews.recent()
    second = consider(reviews, context, hypothesis)
    assert second["status"] == "ready" and second["follows"] == first["candidateId"]


def test_draft_only_after_audio_and_paraphrase_suppression_survives_restart(tmp_path):
    journal, reviews, context, hypothesis = fixture(tmp_path)
    timeline = TimelineMemory(tmp_path / "timeline.sqlite3")
    first = consider(reviews, context, hypothesis)
    assert timeline.pending_draft(user_id="u", case_id="case") is None
    reviews.audio_sent(first["candidateId"], timeline, user_id="u", case_id="case", response_id="actual-response")
    draft = timeline.pending_draft(user_id="u", case_id="case")
    assert draft and draft["sourceIds"] == [first["observationEventId"]]
    reviews.audio_sent(first["candidateId"], timeline, user_id="u", case_id="case", response_id="actual-response")
    assert timeline.pending_draft(user_id="u", case_id="case")["id"] == draft["id"]
    timeline.finish_draft(draft["id"])
    reviews.reconcile(None)
    assert reviews.rows[first["candidateId"]]["status"] == "closed"
    restored = ObservationReviews(SessionJournal(journal.path))
    hypothesis.follow_up_of = first["candidateId"]
    hypothesis.clarification = "Was this another cloth?"
    context.reference_context["questionHistory"] = restored.recent()
    second = consider(restored, context, hypothesis)
    assert second["reason"] == "already-offered"
    hypothesis.follow_up_of = second["candidateId"]
    context.reference_context["questionHistory"] = restored.recent()
    third = consider(restored, context, hypothesis)
    assert third["reason"] == "already-offered"


def test_quiet_and_existing_draft_block_spontaneous_questions(tmp_path):
    _, reviews, context, hypothesis = fixture(tmp_path)
    assert consider(reviews, context, hypothesis, allowed=False)["reason"] == "output-not-permitted"
    assert consider(reviews, context, hypothesis, pending={"id": "user-draft"})["reason"] == "pending-clarification"


def test_blocked_review_keeps_permission_at_decision_time(tmp_path):
    journal, reviews, context, hypothesis = fixture(tmp_path)
    permission = {'quiet': True, 'paused': False, 'permissionSource': {'eventId': 'quiet-1'}}
    row = consider(reviews, context, hypothesis, allowed=False, output_permission=permission)
    assert row['reason'] == 'output-not-permitted' and row['outputPermission'] == permission
    journal.append('interaction.permission', {'quiet': False})
    recovered = ObservationReviews(journal).rows[row['candidateId']]
    assert recovered['outputPermission']['quiet']
    assert recovered['status'] == 'deferred'  # Clearing quiet never replays old evidence.


def test_optional_malformed_comparison_preserves_visible_evidence(tmp_path):
    from egoassist.realtime.perception import parse_ambient_hypothesis
    _, reviews, context, _ = fixture(tmp_path)
    hypothesis = parse_ambient_hypothesis({"summary": "red table", "visible_evidence": ["red cloth"],
        "uncertainties": [], "salient_change": True, "reference_check": {"evidence_indices": []},
        "clarification": "Has it changed?"}, open_observation=True)
    row = consider(reviews, context, hypothesis)
    assert row["referenceStatus"] == "invalid-reference-schema"
    assert row["visibleEvidence"] == ["red cloth"] and row["rawReferenceCheck"] == {"evidence_indices": []}
    assert row["status"] == "suppressed"


def test_proactive_audio_keeps_review_correlation_and_dropped_output_is_deferred():
    from egoassist.realtime.omni import OmniBrowserBridge, _ResponseRequest
    request = _ResponseRequest("ask", 35, 1, origin={"observationReviewId": "review", "generation": 2, "privateContext": "not-a-correlation"})
    assert request.correlation == {"observationReviewId": "review", "generation": 2}
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    calls = []
    bridge._response_lifecycle_handler = lambda origin, status: calls.append((origin, status))
    bridge._return_result_to_inbox(request, "output-permission")
    assert calls == [(request.origin, "suspended")]


def test_same_case_participant_memory_reaches_observer_without_other_users(tmp_path):
    from egoassist.runtime import ThreeLaneRuntime
    timeline = TimelineMemory(tmp_path / "timeline.sqlite3")
    for user, case in (("u", "case"), ("other", "case"), ("u", "other")):
        timeline.remember(user+case, user_id=user, case_id=case, scope="participant", kind="fact")
    framework = ThreeLaneRuntime(SessionJournal(tmp_path / "case.jsonl", session_id="case"), "u")
    assert [row["text"] for row in framework.observation_references(timeline)["memory"]] == ["ucase"]


@pytest.mark.parametrize("previous_kind", ["fact", "event"])
def test_explicit_memory_replacement_is_source_bound_and_atomic(tmp_path, previous_kind):
    from egoassist.realtime.semantic import SemanticTurnRouter
    timeline = TimelineMemory(tmp_path / "timeline.sqlite3")
    old = timeline.remember("blue cloth", user_id="u", case_id="case", scope="participant", kind=previous_kind, subject="table", predicate="color")
    atom = {"kind": "fact", "text": "red cloth", "subject": "table", "predicate": "cloth", "value": "red",
            "scope": "participant", "consent": "explicit", "replaces": old["id"]}
    with pytest.raises(ValueError, match="replacement"):
        SemanticTurnRouter._normalize_atoms([atom])
    assert SemanticTurnRouter._normalize_atoms([atom], reference_cards=[old])[0]["replaces"] == old["id"]
    with pytest.raises(ValueError, match="replacement target"):
        timeline.remember("red cloth", user_id="another", case_id="case", scope="participant", kind="fact", replaces_card_id=old["id"])
    with pytest.raises(TypeError):
        timeline.remember("red cloth", user_id="u", case_id="case", scope="participant", kind="fact", replaces_card_id=old["id"], metadata={"bad": object()})
    assert timeline.cards(user_id="u")[0]["status"] == "active"
    new = timeline.remember("red cloth", user_id="u", case_id="case", scope="participant", kind="fact",
                            subject="table", predicate="cloth", replaces_card_id=old["id"], source_ids=["correction-turn"])
    assert new["supersedes"] == old["id"]
    assert [c["id"] for c in timeline.cards(user_id="u")] == [new["id"]]
    assert timeline.cards(user_id="u", statuses=["superseded"])[0]["id"] == old["id"]


def test_cooldown_is_not_lost_when_many_uninteresting_frames_arrive(tmp_path):
    _, reviews, context, hypothesis = fixture(tmp_path)
    timeline = TimelineMemory(tmp_path / "timeline.sqlite3")
    first = consider(reviews, context, hypothesis)
    reviews.audio_sent(first["candidateId"], timeline, user_id="u", case_id="case", response_id="response")
    timeline.finish_draft(timeline.pending_draft(user_id="u", case_id="case")["id"])
    hypothesis.clarification = None
    for _ in range(70):
        consider(reviews, context, hypothesis)
    hypothesis.clarification = "Another useful question?"
    assert consider(reviews, context, hypothesis, cooldown=3600)["reason"] == "cooldown"


def test_prompt_aliases_preserve_captured_ids_and_resolve_to_original_sources(tmp_path):
    from copy import deepcopy
    from egoassist.product.contracts import AmbientFrameContext
    from egoassist.realtime.perception import ambient_prompt

    _, reviews, context, hypothesis = fixture(tmp_path)
    context.reference_context["userMessages"][0]["referenceId"] = "u1"
    context.reference_context["memory"][0]["referenceId"] = "m1"
    original = deepcopy(context.reference_context)
    captured = AmbientFrameContext(context_id="ctx", frame_id="frame", user_id="u", session_id="case",
        watchlist_id="open", watchlist_version="1", watchlist_provenance="test", watchlist_checksum="sum",
        watch_epoch=0, watch_activities=[], watch_locations=[], observation_mode="open", consent_id="grant",
        frame_captured_at=context.frame_captured_at, reference_context=original)
    prompt = ambient_prompt(captured)
    assert '"sourceId":"u1"' in prompt and '"id":"m1"' in prompt
    assert captured.reference_context == original == context.reference_context
    hypothesis.reference_check["source_ids"] = ["u1", "m1"]
    row = consider(reviews, context, hypothesis)
    assert row["referenceCheck"]["source_ids"] == ["original", "derived"]
    assert row["rawReferenceCheck"]["source_ids"] == ["u1", "m1"]
    assert row["sourceRoots"] == ["original"]
    hypothesis.reference_check["source_ids"] = ["m99"]
    assert consider(reviews, context, hypothesis)["referenceStatus"] == "unknown-reference"


@pytest.mark.parametrize("index", [True, 0.5, "0"])
def test_evidence_indices_require_actual_integers(tmp_path, index):
    _, reviews, context, hypothesis = fixture(tmp_path)
    hypothesis.reference_check["evidence_indices"] = [index]
    assert consider(reviews, context, hypothesis)["referenceStatus"] == "invalid-reference-schema"


def test_memory_replacement_alias_is_scoped_to_supplied_snapshot():
    from egoassist.realtime.semantic import SemanticTurnRouter
    atom = {"kind": "fact", "text": "red cloth", "subject": "table", "predicate": "cloth", "value": "red",
            "scope": "participant", "consent": "explicit", "replaces": "m1"}
    cards = [{"id": "actual-card", "referenceId": "m1", "kind": "fact"}]
    assert SemanticTurnRouter._normalize_atoms([atom], reference_cards=cards)[0]["replaces"] == "actual-card"
    for invalid in ("m99", ["m1"], {"id": "m1"}):
        with pytest.raises(ValueError, match="replacement"):
            SemanticTurnRouter._normalize_atoms([{**atom, "replaces": invalid}], reference_cards=cards)
