"""Cross-sync/async replay must not change the first persisted source authority."""
import json
import pytest
from test_ingest_queue import HUMAN, source


UNKNOWN = {'origin_kind':'unknown','capture_method':'explicit_api_declaration','provenance_strength':'unknown'}


def _model_calls(engine):
    return engine.usage()['totals']['calls']


def test_queued_unknown_cannot_upgrade_via_sync_replay(engine, monkeypatch):
    monkeypatch.setattr(engine, 'start_worker', lambda: None)
    raw = {**source('origin-upgrade'), 'actor':UNKNOWN}
    queued = engine.enqueue_message(**raw)
    with pytest.raises(ValueError, match='actor provenance'):
        engine.ingest_message(**{**raw,'actor':HUMAN})
    assert _model_calls(engine) == 0
    assert engine.store.message(queued['message_id'])['actor']['origin_kind'] == 'unknown'
    assert engine.job_status(queued['job_id'])['status'] == 'queued'
    assert engine.run_next_job()
    job = engine.job_status(queued['job_id'])
    assert job['indexed'] and not job['derived_indexed']
    assert job['semantic_processing'] == 'skipped_origin_unverified'
    assert _model_calls(engine) == 0
    graph = engine._read_backend(engine.project_scope('queue-project')).adapter.graph.records_by_id.values()
    assert all(r.metadata.get('origin_kind') != 'human_authored' for r in graph)


@pytest.mark.parametrize('first,second', [('async','sync'),('sync','async')])
def test_origin_downgrade_conflicts_in_both_ingest_paths(engine, monkeypatch, first, second):
    monkeypatch.setattr(engine, 'start_worker', lambda: None)
    methods={'sync':engine.ingest_message,'async':engine.enqueue_message}
    raw=source('origin-downgrade-'+first)
    result=methods[first](**raw)
    previous=_model_calls(engine)
    with pytest.raises(ValueError, match='actor provenance'):
        methods[second](**{**raw,'actor':UNKNOWN})
    assert _model_calls(engine)==previous
    assert engine.store.message(result['message_id'])['actor']['origin_kind']=='human_authored'


def test_no_actor_legacy_replay_remains_idempotent(engine, monkeypatch):
    monkeypatch.setattr(engine, 'start_worker', lambda: None)
    raw=source('legacy'); raw.pop('actor')
    first=engine.enqueue_message(**raw)
    old=engine.store.message(first['message_id'])['actor']
    assert 'origin_kind' not in old
    assert engine.enqueue_message(**raw)['job_id']==first['job_id']
    committed=engine.ingest_message(**raw)
    assert committed['message_id']==first['message_id']
    assert engine.enqueue_message(**raw)['indexed']
    assert engine.store.message(first['message_id'])['actor']==old
    assert _model_calls(engine)==0
    assert len(engine.store.messages(project_id='queue-project'))==1


def test_same_origin_cannot_change_capture_or_strength(engine, monkeypatch):
    monkeypatch.setattr(engine,'start_worker',lambda:None)
    raw=source('strength')
    engine.enqueue_message(**raw)
    with pytest.raises(ValueError, match='actor provenance'):
        engine.enqueue_message(**{**raw,'actor':{**HUMAN,'provenance_strength':'host_declared'}})
    with pytest.raises(ValueError, match='actor provenance'):
        engine.ingest_message(**{**raw,'actor':{**HUMAN,'capture_method':'host_event_metadata'}})
    assert _model_calls(engine)==0


def test_writer_uses_persisted_actor_even_if_register_boundary_is_bypassed(engine, monkeypatch):
    # Defense-in-depth test, not a mock of the public replay contract tested above.
    monkeypatch.setattr(engine,'start_worker',lambda:None)
    raw={**source('persisted-row'),'actor':UNKNOWN}
    queued=engine.enqueue_message(**raw)
    with engine.store.transaction() as db:
        row=dict(db.execute('SELECT * FROM messages WHERE message_id=?',(queued['message_id'],)).fetchone())
    monkeypatch.setattr(engine.store,'register_message',lambda **_kwargs:(row,False))
    engine.ingest_message(**{**raw,'actor':HUMAN})
    assert _model_calls(engine)==0
    graph=engine._read_backend(engine.project_scope('queue-project')).adapter.graph.records_by_id.values()
    assert all(r.metadata.get('origin_kind') != 'human_authored' for r in graph)
