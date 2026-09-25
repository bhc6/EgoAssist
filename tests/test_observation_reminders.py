from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from egoassist.core.contracts import AmbientObservationHypothesis
from egoassist.memory import TimelineMemory
from egoassist.realtime.context_harness import SessionJournal
from egoassist.realtime.observation_reviews import ObservationReviews
from egoassist.runtime import ThreeLaneRuntime


def setup(tmp_path):
    journal = SessionJournal(tmp_path / 'case.jsonl', session_id='case')
    runtime = ThreeLaneRuntime(journal, 'u')
    timeline = TimelineMemory(tmp_path / 'timeline.sqlite3')
    journal.append('ambient.settings', {'settings': {'reminderGoal': '看到钥匙提醒我收好'}})
    captured = SimpleNamespace(frame_captured_at=datetime.now(timezone.utc), media={'sha256': 'frame'},
                               reference_context=runtime.observation_references(timeline))
    hypothesis = AmbientObservationHypothesis(activity_id='scene', summary='桌上有钥匙', observed=True,
        visible_evidence=['桌上有一串钥匙'], salient_change=False, notification={
            'kind': 'requested-reminder', 'message': '桌上有钥匙，记得收好。',
            'reason': '用户要求看到钥匙时提醒', 'source_ids': ['g1'], 'evidence_indices': [0]})
    return runtime, timeline, captured, hypothesis


def consider(runtime, captured, hypothesis, **options):
    row = runtime.journal.append('ambient_observation', {'observation_id': 'obs-'+str(len(runtime.reviews.rows))})
    return runtime.reviews.consider(captured, hypothesis, row, **{
        'fresh': True, 'enabled': False, 'busy': False, 'allowed': True, 'pending': None, 'cooldown': 90,
        'reminders_enabled': True, **options})


def test_reminder_works_without_questions_and_never_creates_a_memory_draft(tmp_path):
    runtime, timeline, captured, hypothesis = setup(tmp_path)
    row = consider(runtime, captured, hypothesis)
    assert row['status'] == 'ready' and row['question'] is None
    assert row['notification']['source_ids'] == [runtime.observation_goal()['sourceId']]
    runtime.reviews.audio_sent(row['candidateId'], timeline, user_id='u', case_id='case', response_id='speech')
    runtime.reviews.played(row['candidateId'])
    assert runtime.reviews.rows[row['candidateId']]['status'] == 'played'
    assert timeline.pending_draft(user_id='u', case_id='case') is None
    runtime.reviews.reconcile(None)
    assert runtime.reviews.rows[row['candidateId']]['status'] == 'played'


@pytest.mark.parametrize(('option', 'value', 'reason'), [
    ('reminders_enabled', False, 'reminders-disabled'), ('busy', True, 'interaction-busy'),
    ('allowed', False, 'output-not-permitted'), ('fresh', False, 'stale-frame')])
def test_reminders_obey_output_gates(tmp_path, option, value, reason):
    runtime, _, captured, hypothesis = setup(tmp_path)
    options = dict(fresh=True, enabled=False, busy=False, allowed=True, pending=None, cooldown=90, reminders_enabled=True)
    options[option] = value
    row = runtime.journal.append('ambient_observation', {'observation_id': 'obs'})
    assert runtime.reviews.consider(captured, hypothesis, row, **options)['reason'] == reason


@pytest.mark.parametrize(('field', 'value', 'reason'), [
    ('source_ids', ['invented'], 'unknown-reference'), ('source_ids', [], 'missing-user-request'),
    ('evidence_indices', [8], 'unknown-visible-evidence'), ('evidence_indices', [], 'invalid-notification-schema'),
    ('evidence_indices', [True], 'invalid-notification-schema'), ('evidence_indices', [0.2], 'invalid-notification-schema'),
    ('evidence_indices', ['0'], 'invalid-notification-schema'), ('message', ' ', 'invalid-notification-schema'),
    ('reason', '', 'invalid-notification-schema'), ('kind', 'curiosity', 'invalid-notification-schema')])
def test_unsupported_reminders_preserve_evidence_without_speaking(tmp_path, field, value, reason):
    runtime, _, captured, hypothesis = setup(tmp_path)
    hypothesis.notification[field] = value
    row = consider(runtime, captured, hypothesis)
    assert row['status'] == 'suppressed' and row['reason'] == reason
    assert row['visibleEvidence'] == hypothesis.visible_evidence


def test_future_goal_cannot_support_current_frame(tmp_path):
    runtime, _, captured, hypothesis = setup(tmp_path)
    captured.frame_captured_at -= timedelta(seconds=2)
    assert consider(runtime, captured, hypothesis)['reason'] == 'reference-recorded-after-frame'


def test_same_goal_deduplicates_pending_paraphrases_and_survives_restart(tmp_path):
    runtime, timeline, captured, hypothesis = setup(tmp_path)
    first = consider(runtime, captured, hypothesis)
    for message in ('记得拿钥匙。', '请收好桌上的钥匙。'):
        hypothesis.notification['message'] = message
        assert consider(runtime, captured, hypothesis)['reason'] == 'already-queued'
    runtime.reviews.audio_sent(first['candidateId'], timeline, user_id='u', case_id='case', response_id='speech')
    restarted = ThreeLaneRuntime(SessionJournal(runtime.journal.path), 'u')
    hypothesis.salient_change = True
    hypothesis.notification['kind'] = 'goal-help'
    assert consider(restarted, captured, hypothesis)['reason'] == 'already-offered'


def test_unsent_ready_candidate_is_reconsidered_after_restart(tmp_path):
    runtime, _, captured, hypothesis = setup(tmp_path)
    first = consider(runtime, captured, hypothesis)
    restarted = ThreeLaneRuntime(SessionJournal(runtime.journal.path), 'u')
    assert restarted.reviews.rows[first['candidateId']]['status'] == 'deferred'
    assert consider(restarted, captured, hypothesis)['status'] == 'ready'


def test_goal_identity_survives_cadence_change_but_not_clear_and_recreate(tmp_path):
    runtime, _, _, _ = setup(tmp_path)
    first = runtime.observation_goal()
    runtime.journal.append('ambient.settings', {'settings': {'reminderGoal': first['text'], 'intervalSeconds': 20}})
    assert runtime.observation_goal() == first
    assert ThreeLaneRuntime(SessionJournal(runtime.journal.path), 'u').observation_goal() == first
    runtime.journal.append('ambient.settings', {'settings': {'reminderGoal': ''}})
    assert runtime.observation_goal() is None
    runtime.journal.append('ambient.settings', {'settings': {'reminderGoal': first['text']}})
    assert runtime.observation_goal()['sourceId'] != first['sourceId']


def test_hazard_requires_evidence_and_same_frame_cannot_repeat_by_paraphrasing(tmp_path):
    runtime, timeline, captured, hypothesis = setup(tmp_path)
    hypothesis.notification.update(kind='hazard', source_ids=[], message='杯子在桌边，请留意。', reason='杯子倾斜在桌缘')
    hypothesis.visible_evidence = ['杯子倾斜在桌缘']
    row = consider(runtime, captured, hypothesis)
    assert row['status'] == 'ready'
    runtime.reviews.audio_sent(row['candidateId'], timeline, user_id='u', case_id='case', response_id='speech')
    hypothesis.notification['message'] = '请留意桌边的杯子。'
    assert consider(runtime, captured, hypothesis)['reason'] == 'already-offered'


def test_ordinary_scene_has_no_reminder(tmp_path):
    runtime, _, captured, hypothesis = setup(tmp_path)
    hypothesis.notification = None
    assert consider(runtime, captured, hypothesis)['status'] == 'recorded'


def test_changed_goal_does_not_inherit_delivery_and_added_references_do_not_repeat(tmp_path):
    runtime, timeline, captured, hypothesis = setup(tmp_path)
    first = consider(runtime, captured, hypothesis)
    runtime.reviews.audio_sent(first['candidateId'], timeline, user_id='u', case_id='case', response_id='speech')
    captured.reference_context['userMessages'] = [{'sourceId':'extra', 'referenceId':'u1', 'text':'钥匙'}]
    hypothesis.notification['source_ids'] = ['g1','u1']
    assert consider(runtime, captured, hypothesis)['reason'] == 'already-offered'
    runtime.journal.append('ambient.settings', {'settings': {'reminderGoal':'看到钥匙提醒我交给同事'}})
    captured.reference_context = runtime.observation_references(timeline)
    captured.frame_captured_at = datetime.now(timezone.utc)
    hypothesis.notification['source_ids'] = ['g1']
    hypothesis.follow_up_of = first['candidateId']
    assert consider(runtime, captured, hypothesis, cooldown=0)['status'] == 'ready'
