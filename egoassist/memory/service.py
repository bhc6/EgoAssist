"""Memory lane entry point: bounded reads and source-bound writes.

No runtime owner, media transport or interaction router is required here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import time
from typing import ClassVar, Protocol

from egoassist.core.journal import EvidenceUnavailable
from egoassist.core.source_events import project_source
from egoassist.core.source_contract import SourceReference
from .episodes import RealtimeEpisodeMemory
from .metrics import MemoryMetrics, measure_storage
from .timeline import DEFAULT_RETRIEVAL_MIN_SCORE
from .temporal import ClockContext


class MemoryExtractor(Protocol):
    model: str
    def set_turn_context(self, **context): ...
    async def classify(self, text, skills): ...
    def reset_failure(self): ...
    async def aclose(self): ...


@dataclass(frozen=True)
class MemoryQuery:
    schema_version: ClassVar[str] = 'memory-query.v1'
    user_id: str
    session_id: str | None
    text: str
    policy: dict
    clock: ClockContext
    semantic: dict = field(default_factory=dict)
    ranking_query: str | None = None
    exclude: tuple[str, ...] = ()
    source: SourceReference | None = None

    def __post_init__(self):
        object.__setattr__(self, 'policy', deepcopy(self.policy))
        object.__setattr__(self, 'semantic', deepcopy(self.semantic))
        if self.source and (self.source.user_id != self.user_id or self.source.session_id != self.session_id
                            or self.source.source_id not in self.exclude):
            raise ValueError('query source ownership or exclusion mismatch')


@dataclass
class PreparedMemory:
    schema_version: ClassVar[str] = 'prepared-memory.v1'
    request: MemoryQuery
    retrieval: dict | None = None
    participant: bool = False
    evidence: dict | None = None
    failures: dict[str, str] = field(default_factory=dict)
    read_ms: float | None = None


@dataclass(frozen=True)
class ObservationMemory:
    schema_version: ClassVar[str] = 'observation-memory.v1'
    user_id: str
    session_id: str
    source_id: str
    text: str
    source: str
    captured_at: str
    recorded_at: str
    clock: ClockContext
    evidence: dict
    eligible: bool = True
    skill: str | None = None
    task: str | None = None


class MemoryService:
    def __init__(self, timeline, evidence, commit, *, read_timeout=2.0, metrics=None):
        self.timeline, self.evidence, self.writer = timeline, evidence, commit
        self.read_timeout = read_timeout
        self.metrics = metrics or MemoryMetrics()

    def episode(self, user, case, scope):
        return RealtimeEpisodeMemory(self.timeline.path, timeline=self.timeline,
            owner_user_id=user, owner_case_id=case, scope=scope)

    def _retrieve(self, request):
        transcript, user_scope = request.text, request.user_id
        semantic_decision, clock_context = request.semantic, request.clock
        episodic_settings = request.policy['components']['timeline']
        preference_enabled = episodic_settings['preference_enabled']
        active_episodic = self.episode(user_scope, request.session_id,
            episodic_settings['scope'] if request.session_id else 'participant')
        episodic_retrieval = None
        if episodic_settings["episodic_enabled"]:
            episodic_retrieval = active_episodic.retrieve(
                transcript,
                session_id=request.session_id,
                user_scope=user_scope,
                allow_cross_session=episodic_settings["scope"] == "participant",
                referential=semantic_decision.get("memoryQuery") == "referential",
                clock=clock_context,
                query_spec=semantic_decision.get("memoryQuerySpec"),
                project=False,
                ranking_query=request.ranking_query,
            )
        durable_retrieval = None
        if (
            preference_enabled
            and user_scope != "local-default"
            and (
                episodic_retrieval is None
                or getattr(active_episodic, "scope", None) != "participant"
            )
        ):
            durable_retrieval = self.episode(user_scope, request.session_id, "participant").retrieve(
                transcript,
                session_id=request.session_id,
                user_scope=user_scope,
                allow_cross_session=True,
                referential=semantic_decision.get("memoryQuery") == "referential",
                clock=clock_context,
                query_spec=semantic_decision.get("memoryQuerySpec"),
                project=False,
                ranking_query=request.ranking_query,
            )
        if episodic_retrieval is not None and durable_retrieval is not None:
            merged_by_id = {
                item["episodeId"]: item
                for item in episodic_retrieval.get("candidateAudit", episodic_retrieval.get("candidates", []))
                + durable_retrieval.get("candidateAudit", durable_retrieval.get("candidates", []))
            }
            merged_candidates = sorted(
                merged_by_id.values(),
                key=lambda item: (
                    item.get("score", 0.0),
                    item.get("record", {}).get("timestamp", ""),
                ),
                reverse=True,
            )
            minimum_score = max(
                float(
                    episodic_retrieval.get("minimumScore")
                    or DEFAULT_RETRIEVAL_MIN_SCORE
                ),
                float(
                    durable_retrieval.get("minimumScore") or DEFAULT_RETRIEVAL_MIN_SCORE
                ),
            )
            merged_selected = [
                item
                for item in merged_candidates
                if item.get("score", 0.0) >= minimum_score
            ][:5]
            selected_ids = {item["episodeId"] for item in merged_selected}
            for item in merged_candidates:
                item["selected"] = item["episodeId"] in selected_ids
                if (
                    item.get("score")
                    and (not item["selected"])
                    and ("outside-top-k" not in item["rejectionReasons"])
                ):
                    item["rejectionReasons"].append("outside-top-k")
            episodic_retrieval = {
                **episodic_retrieval,
                "scope": {
                    **episodic_retrieval["scope"],
                    "memoryScope": "case+participant",
                    "allowCrossSession": True,
                },
                "candidates": merged_candidates[:20],
                "candidateAudit": merged_candidates,
                "selected": merged_selected,
                "minimumScore": minimum_score,
                "claimBoundary": "Merged same-case event cards and explicitly durable participant cards.",
            }
        elif durable_retrieval is not None:
            episodic_retrieval = {
                **durable_retrieval,
                "scope": {
                    **durable_retrieval["scope"],
                    "memoryScope": "participant",
                    "allowCrossSession": True,
                },
                "claimBoundary": "Explicitly durable participant timeline cards; case episodic memory is disabled.",
            }
        return episodic_retrieval, durable_retrieval is not None

    def read(self, request: MemoryQuery) -> PreparedMemory:
        started = time.perf_counter()
        metrics = self.metrics.for_scope(request.user_id, request.session_id)
        result = PreparedMemory(request)
        try:
            result.retrieval, result.participant = self._retrieve(request)
        except Exception as exc:
            result.failures['timeline'] = type(exc).__name__
        if (request.session_id and request.user_id != 'local-default'
                and request.policy['components']['timeline']['episodic_enabled']):
            try:
                result.evidence = self.evidence.query(request.user_id, request.session_id,
                    request.text, exclude=request.exclude)
            except Exception as exc:
                result.failures['sources'] = type(exc).__name__
        result.read_ms = round((time.perf_counter() - started) * 1000, 3)
        metrics.timing('read', result.read_ms)
        if result.failures:
            for name in result.failures:
                metrics.increment('read.failure.' + name)
        else:
            metrics.increment('read.completed')
        return result

    async def prepare(self, request: MemoryQuery) -> PreparedMemory:
        metrics = self.metrics.for_scope(request.user_id, request.session_id)
        metrics.increment("prepare.requests")
        started = time.perf_counter()
        # EvidenceService owns worker lifetime so privacy maintenance also drains
        # timed-out reads before deleting a source index or account directory.
        try:
            result = await self.evidence.run_read(request.user_id, lambda: self.read(request),
                                                  timeout=self.read_timeout)
            metrics.increment("prepare.degraded" if result.failures else "prepare.completed")
            return result
        except (TimeoutError, EvidenceUnavailable) as exc:
            metrics.increment('prepare.timeout' if isinstance(exc, TimeoutError)
                                   else 'prepare.unavailable')
            metrics.increment('prepare.degraded')
            return PreparedMemory(request, failures={'read': type(exc).__name__})
        except asyncio.CancelledError:
            metrics.increment('prepare.cancelled')
            raise
        finally:
            metrics.timing('prepare', (time.perf_counter() - started) * 1000)

    def accept(self, prepared: PreparedMemory, request: MemoryQuery, journal=None):
        """Revalidate selected evidence only; never rescan the corpus under a case lock."""
        metrics = self.metrics.for_scope(request.user_id, request.session_id)
        metrics.increment("accept.requests")
        if (prepared.request.user_id != request.user_id or prepared.request.session_id != request.session_id
                or prepared.request.text != request.text or prepared.request.policy != request.policy
                or prepared.request.semantic != request.semantic
                or prepared.request.clock != request.clock
                or prepared.request.ranking_query != request.ranking_query
                or prepared.request.exclude != request.exclude
                or prepared.request.source != request.source):
            metrics.increment('accept.rejected.scope')
            metrics.increment('accept.rejected')
            return PreparedMemory(request, failures={'read': 'scope-or-policy-changed'})
        result = deepcopy(prepared)
        if result.retrieval:
            try:
                selected = result.retrieval['selected']
                current = self.timeline.cards_by_ids([x['episodeId'] for x in selected], user_id=request.user_id)
                valid = {cid for cid, card in current.items() if card['status'] == 'active'}
                kept = [x for x in selected if x['episodeId'] in valid
                    and RealtimeEpisodeMemory._record(current[x['episodeId']]) == x['record']]
                if journal:
                    source_ids = {sid for item in kept if item['record'].get('sessionId') == journal.session_id
                                  for sid in item['record'].get('sourceIds', [])}
                    sources = journal.entries_by_id(source_ids)
                    kept = [item for item in kept if item['record'].get('sessionId') != journal.session_id
                            or all(sources.get(sid, {}).get('type') not in {None, 'retention.deleted', 'source.deleted'}
                                   for sid in item['record'].get('sourceIds', []))]
                if len(kept) != len(selected):
                    metrics.increment('accept.rejected.timeline')
                    result.failures['timeline'] = 'selected-memory-changed'
                result.retrieval['selected'] = kept
                ids = {x['episodeId'] for x in kept}
                # Unselected candidates need no second corpus scan. Keep only
                # scores/IDs in their audit so erased text cannot be republished.
                for key in ('candidateAudit', 'candidates'):
                    for item in result.retrieval.get(key, []):
                        if item['episodeId'] not in ids:
                            item['record'] = {}
                            if item.get('selected'):
                                item['selected'] = False
                                item.setdefault('rejectionReasons', []).append('changed-before-commit')
            except Exception as exc:
                metrics.increment('accept.rejected.timeline')
                result.retrieval = None
                result.failures['timeline'] = type(exc).__name__
        if result.evidence:
            try:
                sources = result.evidence['sources'] + result.evidence['conversation']
                if journal is None or journal.session_id != request.session_id:
                    raise EvidenceUnavailable('missing source journal')
                rows = journal.entries_by_id([s['journalEventId'] for s in sources])
                for source in sources:
                    row = rows.get(source['journalEventId'])
                    if row is None or (project_source(row, journal.session_id) or {}).get('sourceHash') != source['sourceHash']:
                        raise EvidenceUnavailable('source changed before commit')
            except Exception as exc:
                metrics.increment('accept.rejected.sources')
                result.evidence = None
                result.failures['sources'] = type(exc).__name__
        if set(result.failures.items()) - set(prepared.failures.items()):
            metrics.increment('accept.rejected')
        return result

    def commit(self, **request):
        return self._operation('commit', request['user_scope'], getattr(request.get('journal'), 'session_id', None),
                               lambda: self.writer.commit(**request))

    def _operation(self, operation, user, case, callback):
        metrics = self.metrics.for_scope(user, case)
        metrics.increment(operation + '.requests')
        started = time.perf_counter()
        try:
            with measure_storage(metrics):
                result = callback()
            metrics.increment(operation + '.completed')
            return result
        except Exception:
            metrics.increment(operation + '.failed')
            raise
        finally:
            metrics.timing(operation, (time.perf_counter() - started) * 1000)

    def preference_retrieval(self, adapter, triggers, *, enabled=True, emit=False):
        if not enabled or not triggers:
            return {'method': 'exact-trigger-v1' if enabled else 'disabled-by-memory-policy',
                    'query': {'task': None, 'checkpoint': None}, 'selected': None, 'candidates': []}
        def retrieve():
            attempts = [adapter.retrieve_preference(task, step, emit=emit and i == 0)
                        for i, (task, step) in enumerate(triggers)]
            return next((item for item in attempts if item['selected']), attempts[0])
        return self._operation('preference.read', adapter.owner_user_id, adapter.owner_case_id, retrieve)

    def propose_preference(self, adapter, **values):
        return self._operation('preference.propose', adapter.owner_user_id, values.get('candidate_scope'),
                               lambda: adapter.propose_preference(**values))

    def resolve_preference(self, adapter, accepted, *, candidate_scope=None):
        return self._operation('preference.resolve', adapter.owner_user_id, candidate_scope,
                               lambda: adapter.resolve_candidate(accepted, candidate_scope=candidate_scope))

    def delete_preference(self, adapter, record_id, *, candidate_scope=None):
        return self._operation('preference.delete', adapter.owner_user_id, candidate_scope,
                               lambda: adapter.delete_preference(record_id, candidate_scope=candidate_scope))

    def snapshot_preference(self, adapter, **scope):
        return adapter.snapshot_preference(**scope)

    def snapshot_episode(self, adapter, **scope):
        return adapter.snapshot(**scope)

    def consolidate(self, user, case):
        return self._operation('consolidate', user, case, lambda: [
            self.timeline.promote_to_long(user_id=user, case_id=case, scope='case'),
            self.timeline.promote_to_long(user_id=user, case_id=None, scope='participant')])

    def pending_draft(self, user_id, case_id):
        return self.timeline.pending_draft(user_id=user_id, case_id=case_id)

    def begin_draft(self, **request):
        return self._operation('draft.write', request['user_id'], request['case_id'],
                               lambda: self.timeline.begin_draft(**request))

    def delete_card(self, user, case, card_id):
        card = self.timeline.cards_by_ids([card_id], user_id=user).get(card_id)
        if not card or card['status'] not in {'active', 'pending'} or (
                card['scope'] != 'participant' and card.get('caseId') != case):
            raise ValueError('memory not found')
        return self._operation('card.delete', user, case, lambda: self.timeline.delete(card_id))

    def status(self, user, case=None):
        return {**self.metrics.snapshot(user, case), 'readWorkers': self.evidence.status(user)}

    def remember_scene(self, adapter, payload, **values):
        return self._operation('scene.write', values.get('user_id', 'local-default'), values.get('session_id'),
                               lambda: self._remember_scene(adapter, payload, **values))

    def _remember_scene(self, adapter, payload, *, policy, source_id, user_id='local-default',
                       session_id=None, skill=None, task=None):
        settings = policy['components']['timeline']
        if not settings['episodic_enabled']:
            return None
        recent = adapter.snapshot(session_id=session_id, user_scope=user_id,
            allow_cross_session=settings['scope'] == 'participant')['recent']
        if not payload.get('salientChange') and any(item['kind'] == 'scene' for item in recent):
            return None
        return adapter.remember(payload.get('sceneSummary', ''), kind='scene', source='proactive-visual-gate',
            skill=skill, task=task, evidence_event_id=source_id, session_id=session_id, user_scope=user_id)

    def remember_observation(self, request: ObservationMemory, policy):
        return self._operation('observation.write', request.user_id, request.session_id,
                               lambda: self._remember_observation(request, policy))

    def _remember_observation(self, request, policy):
        if not request.eligible or not policy['components']['timeline']['episodic_enabled']:
            return None
        return self.episode(request.user_id, request.session_id, policy['components']['timeline']['scope']).remember(
            request.text, kind='observation', source=request.source, skill=request.skill, task=request.task,
            session_id=request.session_id, user_scope=request.user_id, evidence_event_id=request.source_id,
            happened_at=request.captured_at, recorded_at=request.recorded_at,
            timezone_name=request.clock.timezone_name, utc_offset_minutes=request.clock.utc_offset_minutes,
            evidence_context=request.evidence)

    def remember_tool_effect(self, *, user_id, session_id, call_id, name, result, source_id):
        return self._operation('tool.write', user_id, session_id, lambda: self._remember_tool_effect(
            user_id=user_id, session_id=session_id, call_id=call_id, name=name, result=result, source_id=source_id))

    def _remember_tool_effect(self, *, user_id, session_id, call_id, name, result, source_id):
        import json
        return self.timeline.remember('Tool ' + name + ' completed: ' + json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
            user_id=user_id, case_id=session_id, scope='case', kind='tool-effect', tier='short',
            memory_key=f'tool-effect:{name}:{call_id}', source_ids=[source_id] if source_id else [], importance=0.8,
            metadata={'callId':call_id, 'tool':name, 'result':dict(result)})
