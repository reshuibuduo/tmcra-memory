from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import FakeMemoryModel, build_test_engine, local_config
from tmcra_local.api import create_app
from tmcra_local.engine import LocalMemoryEngine
from tmcra_local.ingest_queue import QueueError, SourceChanged
from tmcra_local.runtime_modes import RuntimeModeError
from tmcra_local.generation_runtime import managed_local_generation

HUMAN = {'origin_kind': 'human_authored', 'capture_method': 'explicit_api_declaration', 'provenance_strength': 'caller_declared'}


def source(identifier='a', project='queue-project', **extra):
    return dict(project_id=project, session_id=project + '-session', role='user',
                content='Synthetic QUEUE evidence ' + identifier, source_app='test',
                native_thread_id=project + '-session', native_message_id=identifier,
                actor=HUMAN, **extra)


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('condition was not reached before bounded timeout')


class SlowModel(FakeMemoryModel):
    def __init__(self, entered, release, usage_sink=None):
        super().__init__(usage_sink)
        self.entered, self.release = entered, release
        self.calls = 0

    def complete(self, payload):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(8), 'test failed to release fake writer'
        return super().complete(payload)


def slow(engine):
    entered, release = threading.Event(), threading.Event()
    model = SlowModel(entered, release, engine._record_usage)
    engine.writer.flash_client = model
    return entered, release, model


def test_raw_and_job_atomic_idempotent_and_visibility_conflict(engine, monkeypatch):
    monkeypatch.setattr(engine, 'start_worker', lambda: None)
    first = engine.enqueue_message(**source())
    assert first['accepted'] and not first['indexed'] and not first['derived_indexed']
    assert first['status'] == 'queued'
    assert engine.store.message(first['message_id'])['content'] == source()['content']
    again = engine.enqueue_message(**source())
    assert again['job_id'] == first['job_id']
    with pytest.raises(QueueError, match='visibility'):
        engine.enqueue_message(**source(visibility='both'))
    assert len(engine.store.messages(project_id='queue-project')) == 1
    with pytest.raises(ValueError, match='different content'):
        changed = source(); changed['content'] = 'conflict'
        engine.enqueue_message(**changed)
    admission = engine.jobs.admission
    def fail(_scopes):
        def rejected(*_):
            raise RuntimeError('simulate job persistence failure')
        return rejected
    monkeypatch.setattr(engine.jobs, 'admission', fail)
    with pytest.raises(RuntimeError):
        engine.enqueue_message(**source('rollback'))
    assert len(engine.store.messages(project_id='queue-project')) == 1
    monkeypatch.setattr(engine.jobs, 'admission', admission)


def test_slow_inference_does_not_block_admission_recall_or_workflow(engine):
    engine.ingest_message(**source('existing'))
    entered, release, model = slow(engine)
    try:
        started = time.monotonic()
        first = engine.enqueue_message(**source('slow'))
        assert time.monotonic() - started < .7
        assert entered.wait(5)
        assert engine.job_status(first['job_id'])['status'] == 'running'
        started = time.monotonic()
        read = engine.recall(project_id='queue-project', query='existing QUEUE')
        workflow = engine.workflows.snapshot('queue-project')
        second = engine.enqueue_message(**source('next'))
        assert time.monotonic() - started < 1.5
        assert read['evidence_windows'] and workflow['revision'] == 0
        assert not second['indexed']
        release.set()
        until(lambda: engine.job_status(second['job_id'])['indexed'])
        assert model.calls == 2
    finally:
        release.set(); engine.close()


def test_legacy_sync_waits_for_indexing_not_accepted(engine):
    entered, release, _ = slow(engine)
    result = []
    thread = threading.Thread(target=lambda: result.append(engine.ingest_message(**source('legacy'))))
    thread.start()
    assert entered.wait(5) and result == []
    release.set(); thread.join(5)
    assert result[0]['scopes'][0]['status'] == 'committed'


def test_read_factory_never_exposes_uncommitted_writer_graph(engine):
    engine.ingest_message(**source('existing'))
    backend = engine.graph_factory.for_scope(engine.project_scope('queue-project'))
    with backend.mutation_batch():
        record = next(iter(backend.adapter.graph.records_by_id.values()))
        old = record.value
        record.value = 'UNCOMMITTED_SENTINEL'
        read = engine.recall(project_id='queue-project', query='existing QUEUE')
        assert 'UNCOMMITTED_SENTINEL' not in json.dumps(read)
        assert read['evidence_windows']
        record.value = old


def test_delete_in_flight_marks_tombstone_then_rejects_late_model(engine):
    entered, release, _ = slow(engine)
    first = engine.enqueue_message(**source('revoked'))
    assert entered.wait(5)
    deleted, errors = [], []
    def remove():
        try:
            deleted.append(engine.delete_message(first['message_id']))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=remove)
    thread.start()
    try:
        until(lambda: bool(engine.store.message(first['message_id']).get('deleted_at')))
        assert engine.recall(project_id='queue-project', query='revoked')['evidence_windows'] == []
        release.set(); thread.join(8)
        assert not errors and deleted[0]['deleted']
        assert engine.job_status(first['job_id'])['status'] == 'cancelled'
        assert engine.store.message(first['message_id']) is None
        assert engine.recall(project_id='queue-project', query='revoked')['evidence_windows'] == []
        with pytest.raises(ValueError, match='revoked'):
            engine.enqueue_message(**source('revoked'))
    finally:
        release.set(); thread.join(8); engine.close()


def test_permanent_failure_does_not_poison_following_job(engine):
    original = engine.writer.flash_client
    class Selective(FakeMemoryModel):
        def complete(self, payload):
            if 'bad' in json.dumps(payload):
                raise ValueError('SECRET echo that must not enter job record')
            return original.complete(payload)
    engine.writer.flash_client = Selective()
    try:
        first = engine.enqueue_message(**source('bad'))
        second = engine.enqueue_message(**source('good'))
        until(lambda: engine.job_status(second['job_id'])['indexed'])
        bad = engine.job_status(first['job_id'])
        assert bad['status'] == 'permanent_failed' and bad['attempts'] == 1
        assert 'SECRET' not in json.dumps(bad)
        scopes = engine.store.scopes_for_message(first['message_id'])
        assert scopes and scopes[0]['error'] == 'validation_failed'
        assert 'SECRET' not in json.dumps(scopes)
        with pytest.raises(QueueError, match='confirmation'):
            engine.retry_job(first['job_id'])
        engine.writer.flash_client = original
        engine.retry_job(first['job_id'], confirmed=True)
        until(lambda: engine.job_status(first['job_id'])['indexed'])
        assert engine.job_status(first['job_id'])['attempts'] == 2
    finally:
        engine.close()


def test_same_timestamp_queue_order_is_admission_order(engine, monkeypatch):
    monkeypatch.setattr(engine, 'start_worker', lambda: None)
    monkeypatch.setattr('tmcra_local.ingest_queue.utc_now', lambda: '2026-01-01T00:00:00Z')
    jobs = [engine.enqueue_message(**source(identifier)) for identifier in ['z', 'a', 'm']]
    with engine._writer_lease.acquire():
        for expected in jobs:
            actual = engine.jobs.claim()
            assert actual['job_id'] == expected['job_id']
            engine.jobs.finish(actual['message_id'], 'permanent_failed', 'fixture')


def _crash_worker(config, marker):
    engine = build_test_engine(Path(config))
    class Block(FakeMemoryModel):
        def complete(self, _payload):
            Path(marker).write_text('model call entered')
            time.sleep(60)
            raise AssertionError('parent should kill this process')
    engine.writer.flash_client = Block()
    engine.enqueue_message(**source('crash'))
    while True:
        time.sleep(1)


def test_killed_process_recovery_never_replays_unknown_call(tmp_path):
    config = local_config(tmp_path)
    marker = tmp_path / 'entered'
    context = multiprocessing.get_context('spawn')
    process = context.Process(target=_crash_worker, args=(str(config), str(marker)))
    process.start()
    try:
        until(marker.exists, timeout=15)
        process.kill(); process.join(5)
        assert process.exitcode != 0
        engine = build_test_engine(config)
        engine.start_worker = lambda: None
        job = engine.enqueue_message(**source('crash'))
        assert job['status'] == 'running'
        assert engine.run_next_job() is False
        recovered = engine.job_status(job['job_id'])
        assert recovered['status'] == 'interrupted' and recovered['attempts'] == 1
        assert engine.usage()['totals']['calls'] == 0
        engine.retry_job(job['job_id'], confirmed=True)
        assert engine.run_next_job()
        assert engine.job_status(job['job_id'])['indexed']
        assert engine.job_status(job['job_id'])['attempts'] == 2
        assert len(engine.store.messages(project_id='queue-project')) == 1
    finally:
        if process.is_alive():
            process.kill(); process.join(5)


def test_read_only_has_no_generation_initialization_or_worker(config_path, monkeypatch):
    original = build_test_engine(config_path)
    original.ingest_message(**source('readable'))
    original.close()
    def forbidden(*_args, **_kwargs):
        raise AssertionError('read-only must never touch a model or generation contract')
    monkeypatch.setattr('tmcra_local.engine.OpenAICompatibleClient', forbidden)
    monkeypatch.setattr('tmcra_local.engine.build_service_environment', forbidden)
    monkeypatch.setattr('tmcra_local.generation_runtime._generation_contract', forbidden)
    monkeypatch.setattr('tmcra_local.generation_runtime.subprocess.Popen', forbidden)
    with managed_local_generation(config_path, runtime_mode_override='read-only') as state:
        assert state['source'] == 'disabled'
    app = create_app(config_path, runtime_mode_override='read-only')
    engine = app.state.engine
    token = Path(app.state.local_token_path).read_text().strip()
    auth = {'Authorization': 'Bearer ' + token}
    with TestClient(app) as client:
        assert engine.llm is None and engine.writer is None and engine._worker is None
        assert client.get('/v1/health').json()['runtime_mode'] == 'read-only'
        assert client.get('/v1/projects/queue-project/workflows', headers=auth).status_code == 200
        assert client.post('/v1/recall', headers=auth, json={'project_id':'queue-project','query':'readable'}).json()['evidence_windows']
        for url, body in [('/v1/messages', source()), ('/v1/message-jobs', source()),
                          ('/v1/projects/queue-project/workflows', {'action':'task.upsert','operation_id':'x','payload':{'id':'x','title':'x'}})]:
            response = client.post(url, headers=auth, json=body)
            assert response.status_code == 403, response.text
            assert response.json()['error'] == 'runtime_mode_disabled'
        assert client.delete('/v1/projects/queue-project', headers=auth).status_code == 403
    with pytest.raises(RuntimeModeError):
        create_app(config_path, runtime_mode_override='off')
    with pytest.raises(RuntimeModeError):
        with managed_local_generation(config_path, runtime_mode_override='off'):
            pass


def test_queue_http_opt_in_and_unknown_source_only(config_path, monkeypatch):
    engine = build_test_engine(config_path)
    monkeypatch.setattr('tmcra_local.api.LocalMemoryEngine', lambda *_a, **_k: engine)
    app = create_app(config_path, verify_models=False)
    auth = {'Authorization':'Bearer ' + Path(app.state.local_token_path).read_text().strip()}
    with TestClient(app) as client:
        assert client.post('/v1/message-jobs', json=source()).status_code == 401
        payload = source(); payload['actor'] = {}
        response = client.post('/v1/message-jobs', headers=auth, json=payload)
        assert response.status_code == 202
        job = response.json()
        until(lambda: engine.job_status(job['job_id'])['indexed'])
        status = client.get('/v1/message-jobs/' + job['job_id'], headers=auth).json()
        assert status['semantic_processing'] == 'skipped_origin_unverified'
        assert status['derived_indexed'] is False
        assert engine.usage()['totals']['calls'] == 0
    assert not engine._worker.is_alive()


def test_retryable_timeout_and_retry_budget(engine, monkeypatch):
    monkeypatch.setattr(engine, 'start_worker', lambda: None)
    class Timeout(FakeMemoryModel):
        def complete(self, payload):
            raise TimeoutError('synthetic timeout')
    engine.writer.flash_client = Timeout()
    job = engine.enqueue_message(**source('timeout'))
    for attempt in range(1, 4):
        assert engine.run_next_job()
        result = engine.job_status(job['job_id'])
        assert result['status'] == 'retryable_failed' and result['attempts'] == attempt
        assert engine.run_next_job() is False
        if attempt < 3:
            engine.retry_job(job['job_id'], confirmed=True)
    with pytest.raises(QueueError, match='budget'):
        engine.retry_job(job['job_id'], confirmed=True)


def test_identity_metadata_is_atomic_and_conflict_checked(engine, monkeypatch):
    monkeypatch.setattr(engine, 'start_worker', lambda: None)
    identity = {'entity_bindings':[{'entity_id':'project:jilan', 'entity_keys':['project.jilan'], 'aliases':['霁蓝']} ]}
    job = engine.enqueue_message(**source('identity', identity_metadata=identity))
    assert engine.store.message(job['message_id'])['identity_metadata'] == {**identity, 'corrections': []}
    with pytest.raises(ValueError, match='different content'):
        engine.enqueue_message(**source('identity', identity_metadata={}))
    assert engine.run_next_job() and engine.job_status(job['job_id'])['indexed']


def test_deleting_queued_project_cancels_jobs_and_preserves_other_project(engine, monkeypatch):
    monkeypatch.setattr(engine, 'start_worker', lambda: None)
    first = engine.enqueue_message(**source('queued', 'first'))
    second = engine.enqueue_message(**source('other', 'second'))
    deleted = engine.delete_project('first')
    assert deleted['deleted_messages'] == 1
    assert engine.job_status(first['job_id'])['status'] == 'cancelled'
    assert engine.run_next_job()
    assert engine.job_status(second['job_id'])['indexed']
    with pytest.raises(ValueError, match='revoked'):
        engine.enqueue_message(**source('queued', 'first'))


def test_stale_knowledge_cache_does_not_silently_upgrade_origin(engine):
    engine.ingest_message(**source('knowledge'))
    engine.store.save_knowledge(scope_id=engine.project_scope('queue-project'),
        source_fingerprint='old-cache-without-origin', payload={'status':'ready','secret':'stale'}, generator='old')
    assert engine.knowledge(project_id='queue-project') is None


def test_nested_graph_lock_is_fully_released_only_for_model(engine):
    entered, release, _ = slow(engine)
    result = []
    def write():
        with engine._lock:
            result.append(engine.ingest_message(**source('nested')))
    thread = threading.Thread(target=write)
    thread.start()
    assert entered.wait(5)
    acquired = threading.Event()
    def read():
        with engine._lock:
            acquired.set()
    reader = threading.Thread(target=read); reader.start()
    try:
        assert acquired.wait(1), 'all owned graph-lock levels must suspend during inference'
    finally:
        release.set(); thread.join(5); reader.join(5)
    assert result[0]['scopes'][0]['status'] == 'committed'


def test_cli_mode_off_no_process_and_read_only_probe_rejected(config_path, monkeypatch, capsys):
    from tmcra_local.__main__ import main
    from tmcra_local.generation_runtime import probe_generation_engine
    def forbidden(*args, **kwargs):
        raise AssertionError('must not start generation')
    monkeypatch.setattr('tmcra_local.generation_runtime.subprocess.Popen', forbidden)
    assert main(['start','--config',str(config_path),'--mode','off']) == 2
    assert 'off runtime mode' in capsys.readouterr().err
    payload = json.loads(config_path.read_text()); payload['runtime_mode'] = 'read-only'
    payload.pop('generation', None); payload.pop('llm', None)
    config_path.write_text(json.dumps(payload))
    assert main(['doctor','--config',str(config_path),'--json']) == 0
    assert json.loads(capsys.readouterr().out)['checks']['model_initialization'] == 'not_performed'
    assert main(['doctor','--config',str(config_path),'--probe-generation']) == 2
    with pytest.raises(RuntimeModeError):
        probe_generation_engine(config_path)


def test_other_source_deletion_invalidates_output_but_allows_explicit_retry(engine):
    old = engine.ingest_message(**source('old', project='separate-project'))
    entered, release, _ = slow(engine)
    current = engine.enqueue_message(**source('surviving'))
    assert entered.wait(5)
    errors = []
    def delete_other():
        try:
            engine.delete_message(old['message_id'])
        except Exception as exc:
            errors.append(exc)
    removal = threading.Thread(target=delete_other)
    removal.start()
    try:
        until(lambda: bool(engine.store.message(old['message_id']).get('deleted_at')))
        release.set(); removal.join(8)
        assert not errors
        until(lambda: engine.job_status(current['job_id'])['status'] == 'interrupted')
        receipt = engine.job_status(current['job_id'])
        assert receipt['error_code'] == 'context_changed'
        assert not engine.store.message(current['message_id'])['deleted_at']
        engine.retry_job(current['job_id'], confirmed=True)
        until(lambda: engine.job_status(current['job_id'])['indexed'])
        assert engine.job_status(current['job_id'])['attempts'] == 2
    finally:
        release.set(); removal.join(8); engine.close()


def test_real_derived_commit_and_reconciliation_reads_committed_snapshot(engine):
    from test_entity_identity import ScriptedModel
    model = ScriptedModel()
    entered, release = threading.Event(), threading.Event()
    original = model.reconcile
    def reconciliation(payload):
        entered.set()
        assert release.wait(8), 'test must release reconciliation'
        return original(payload)
    model.reconcile = reconciliation
    engine.writer.flash_client = engine.writer.pro_client = model
    identity = {'entity_bindings':[{'entity_id':'device:queue-phone',
        'entity_keys':['user.phone','user.handset'],'aliases':['手机','handset']}]}
    old_text, new_text = 'My phone is at the old office.', 'Correction: my handset is at the new home.'
    model.rows['derived-old'] = {'key':'user.phone','text':old_text}
    model.rows['derived-new'] = {'key':'user.handset','text':new_text}
    try:
        old = engine.enqueue_message(**{**source('derived-old'), 'message_id':'derived-old',
            'content':old_text, 'identity_metadata':identity})
        until(lambda: engine.job_status(old['job_id'])['indexed'])
        receipt = engine.job_status(old['job_id'])
        assert receipt['derived_indexed'] and receipt['semantic_processing'] == 'completed'
        new = engine.enqueue_message(**{**source('derived-new'), 'message_id':'derived-new',
            'content':new_text, 'identity_metadata':identity})
        assert entered.wait(5)
        started = time.monotonic()
        view = engine.recall(project_id='queue-project', query='phone office')
        assert time.monotonic() - started < 1.5
        assert view['evidence_windows']
        with engine._lock:
            records = engine._read_backend(engine.project_scope('queue-project')).adapter.graph.records_by_id.values()
            semantic = [r for r in records if r.metadata.get('content_variant') == 'product_semantic_memory']
            assert any(r.value == old_text and r.state == 'active' for r in semantic)
            assert not any(r.value == new_text for r in semantic), 'uncommitted replacement leaked'
        release.set()
        until(lambda: engine.job_status(new['job_id'])['indexed'])
        assert engine.job_status(new['job_id'])['derived_indexed']
        with engine._lock:
            records = engine._read_backend(engine.project_scope('queue-project')).adapter.graph.records_by_id.values()
            semantic = [r for r in records if r.metadata.get('content_variant') == 'product_semantic_memory']
            assert any(r.value == old_text and r.state == 'superseded' for r in semantic)
            assert any(r.value == new_text and r.state == 'active' for r in semantic)
    finally:
        release.set(); engine.close()



def test_windows_writer_lease_retries_contention_without_ten_second_cap(monkeypatch):
    import errno
    import sys
    from types import SimpleNamespace
    from tmcra_local.ingest_queue import ProcessWriterLease
    calls, waits = [], []
    def locking(fd, mode, count):
        calls.append((fd, mode, count))
        if len(calls) <= 12:
            raise OSError(errno.EACCES, 'synthetic lock contention')
    fake = SimpleNamespace(LK_NBLCK=2, locking=locking)
    monkeypatch.setitem(sys.modules, 'msvcrt', fake)
    monkeypatch.setattr('tmcra_local.ingest_queue.time.sleep', waits.append)
    handle = SimpleNamespace(fileno=lambda: 99)
    assert ProcessWriterLease._lock_windows(handle, blocking=True)
    assert len(calls) == 13 and len(waits) == 12
    calls.clear()
    assert not ProcessWriterLease._lock_windows(handle, blocking=False)
    assert len(calls) == 1
    def invalid(*_args):
        raise OSError(errno.EBADF, 'invalid descriptor must not be retried')
    fake.locking = invalid
    with pytest.raises(OSError):
        ProcessWriterLease._lock_windows(handle, blocking=True)
