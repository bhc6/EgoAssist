"""Freeze ownership, telemetry, idempotency and failure reporting at the facade."""
import asyncio
from dataclasses import replace
import json
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from egoassist.core.source_contract import SourceReference
from egoassist.core.events import owned_identifiers, retention_category
from egoassist.memory.metrics import MemoryMetrics, MeasuredConnection, measure_storage
from egoassist.memory.service import MemoryQuery
from egoassist.memory.policy import default_memory_policy
from egoassist.memory.temporal import ClockContext
from test_lane_isolation import memory_fixture


def test_source_contract_keeps_unknown_time_and_separates_account_and_input(tmp_path):
    memory, query, journal, _ = memory_fixture(tmp_path)
    entry = journal.append('source.event', {'source': {'source_at': None}}, idempotency_key='history:unknown')
    ref = SourceReference.from_entry('me', 'case', entry)
    assert ref.source_at is None and ref.idempotency_key == 'history:unknown'
    request = replace(query, source=ref, exclude=(ref.source_id,))
    assert request.source.schema_version == 'source-reference.v1'
    with pytest.raises(ValueError, match='ownership'):
        replace(request, user_id='other')
    with pytest.raises(ValueError, match='exclusion'):
        replace(request, exclude=())
    policy = default_memory_policy()
    semantic = {'memoryQuerySpec': {'subject': 'Alice'}}
    query = MemoryQuery('me', 'case', 'bicycle', policy, ClockContext.current(), semantic)
    policy['components']['timeline']['episodic_enabled'] = False
    semantic['memoryQuerySpec']['subject'] = 'Bob'
    assert query.policy['components']['timeline']['episodic_enabled']
    assert query.semantic['memoryQuerySpec']['subject'] == 'Alice'


def test_long_event_lifecycle_is_memory_but_job_does_not_own_request():
    row = {'id': 'job-row', 'type': 'long_event.job', 'requestId': 'original-request'}
    assert retention_category(row) == 'memory'
    assert owned_identifiers(row) == {'job-row'}


def test_metrics_are_scoped_bounded_and_have_explicit_denominators():
    metrics = MemoryMetrics(capacity=2)
    first = metrics.for_scope('alice', 'one')
    first.increment('prepare.requests', 4)
    first.increment('prepare.timeout')
    first.increment('prepare.degraded', 2)
    for n in range(300):
        first.timing('prepare', n)
    snap = metrics.snapshot('alice', 'one')
    assert snap['rates']['timeoutPerPrepare'] == .25
    assert snap['rates']['degradedPerPrepare'] == .5
    assert snap['rates']['failurePerCommit'] is None
    assert snap['timings']['prepare']['recentSamples'] == 256
    assert metrics.snapshot('bob', 'one')['counts'] == {}
    assert metrics.snapshot('alice', 'two')['counts'] == {}
    assert metrics.snapshot('alice', 'one')['counts'] == {}  # bounded LRU eviction
    assert 'bicycle' not in json.dumps(snap)


def test_busy_sqlite_is_measured_and_keeps_original_error(tmp_path):
    path = tmp_path / 'locked.sqlite3'
    holder = sqlite3.connect(path)
    writer = sqlite3.connect(path, factory=MeasuredConnection, timeout=0.01)
    metrics = MemoryMetrics().for_scope('me', 'case')
    try:
        holder.execute('CREATE TABLE item(id INTEGER PRIMARY KEY)')
        holder.execute('BEGIN IMMEDIATE')
        with measure_storage(metrics):
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                writer.execute('BEGIN IMMEDIATE')
        snap = metrics.snapshot()
        assert snap['counts']['sqlite.busyErrors'] == 1
        assert snap['timings']['sqlite.writerAcquire']['count'] == 1
        holder.rollback()
        with measure_storage(metrics):
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('INSERT INTO item VALUES(1)')
            writer.commit()
        assert metrics.snapshot()['timings']['sqlite.writerAcquire']['count'] == 2
        assert holder.execute('SELECT id FROM item').fetchone()[0] == 1
    finally:
        writer.close()
        holder.close()


def test_failed_commit_is_not_success_and_scope_rejection_is_counted(tmp_path):
    memory, query, journal, _ = memory_fixture(tmp_path)
    def fail(**request):
        raise OSError('injected write failure')
    memory.writer = SimpleNamespace(commit=fail)
    with pytest.raises(OSError):
        memory.commit(user_scope='me', journal=journal)
    prepared = memory.read(query)
    memory.accept(prepared, replace(query, text='different'), journal)
    snap = memory.status('me', 'case')
    assert snap['rates']['failurePerCommit'] == 1
    assert snap['rates']['rejectedPerAccept'] == 1
    assert snap['counts'].get('commit.completed', 0) == 0
    assert memory.status('other', 'case')['counts'] == {}


def test_facade_delete_checks_ownership_and_tool_retries_are_idempotent(tmp_path):
    memory, _, _, card = memory_fixture(tmp_path)
    with pytest.raises(ValueError):
        memory.delete_card('other', 'case', card['id'])
    with pytest.raises(ValueError):
        memory.delete_card('me', 'other-case', card['id'])
    kwargs = dict(user_id='me', session_id='case', call_id='call', name='read-file',
                  result={'status': 'completed'}, source_id='tool-source')
    first = memory.remember_tool_effect(**kwargs)
    second = memory.remember_tool_effect(**kwargs)
    assert first['id'] == second['id']
    assert memory.delete_card('me', 'case', card['id'])['status'] == 'deleted'
    assert memory.status('me', 'case')['timings']['sqlite.writerAcquire']['count'] >= 2


def test_prepare_timeout_is_distinct_from_late_worker_completion(tmp_path):
    async def run():
        memory, query, _, _ = memory_fixture(tmp_path)
        release = Event()
        original = memory.read
        def delayed(request):
            assert release.wait(5)
            return original(request)
        memory.read = delayed
        try:
            result = await memory.prepare(query)
            assert result.failures == {'read': 'TimeoutError'}
            assert memory.status('me', 'case')['readWorkers']['active'] == 1
        finally:
            release.set()
            await memory.evidence.close()
        snap = memory.status('me', 'case')
        assert snap['counts']['prepare.degraded'] == 1
        assert snap['counts'].get('prepare.completed', 0) == 0
        assert snap['counts']['read.completed'] == 1
        assert snap['readWorkers']['active'] == 0
    asyncio.run(run())
