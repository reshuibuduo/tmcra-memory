from contextlib import closing
import sqlite3

import pytest

from tmcra_local.storage import LocalStore
from test_engine import _ingest


def register(store, **overrides):
    values = dict(project_id='p', project_title='Project', session_id='s', session_title='Session',
        role='user', content='synthetic private source', occurred_at='now', source_app='codex',
        native_thread_id='native-thread', native_message_id='native-message', actor={}, message_id='caller-id')
    values.update(overrides)
    return store.register_message(**values)


@pytest.mark.parametrize('delete_kind', ['message', 'project', 'soft'])
def test_deleted_source_cannot_be_reimported_with_new_routing(tmp_path, delete_kind):
    path = tmp_path/'local.sqlite3'
    store = LocalStore(path)
    message, _ = register(store)
    if delete_kind == 'message': store.erase_message_metadata(message['message_id'])
    elif delete_kind == 'project': store.delete_project_metadata('p')
    else: store.mark_message_deleted(message['message_id'])
    reopened = LocalStore(path)
    for overrides in ({}, {'message_id': 'different-id'}, {'project_id': 'new-project', 'session_id': 'new-session'},
                      {'native_message_id': 'new-native-id'}):
        with pytest.raises(ValueError, match='permanently revoked'):
            register(reopened, **overrides)
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute('SELECT * FROM source_tombstones').fetchall()
    assert len(rows) == 2
    for sensitive in ('caller-id','native-thread','native-message','synthetic private source'):
        assert sensitive not in repr(rows)
    # This is identity revocation, not a fuzzy/content blacklist.
    _, created = register(reopened, message_id='fresh-id', native_message_id='fresh-native')
    assert created


def test_deletion_and_tombstone_are_one_transaction(tmp_path):
    store = LocalStore(tmp_path/'local.sqlite3')
    register(store)
    with store.transaction(immediate=True) as db:
        db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError, match='test rollback'):
        store.erase_message_metadata('caller-id')
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute('SELECT COUNT(*) FROM source_tombstones').fetchone()[0] == 0
    assert store.message('caller-id') is not None


def test_project_delete_does_not_revoke_other_projects(tmp_path):
    store = LocalStore(tmp_path/'local.sqlite3')
    register(store)
    register(store, project_id='other', session_id='other-session', native_message_id='other-native', message_id='other-id')
    store.delete_project_metadata('p')
    _, created = register(store, project_id='other', session_id='other-session', native_message_id='other-native', message_id='other-id')
    assert not created


@pytest.mark.parametrize('kind', ['message', 'project'])
def test_engine_delete_blocks_hook_replay_and_invalidates_global_cache(engine, kind):
    args=dict(project='delete-p',session='delete-s',role='user',content='synthetic source',app='codex',native_id='n1',visibility='both')
    message = _ingest(engine, **args)
    engine.store.save_knowledge(scope_id=engine.global_scope(),source_fingerprint='test',payload={'source':'synthetic source'},generator='test')
    if kind == 'message': engine.delete_message(message['message_id'])
    else: engine.delete_project('delete-p')
    assert engine.store.knowledge(engine.global_scope()) is None
    with pytest.raises(ValueError, match='permanently revoked'):
        _ingest(engine, **args)


def test_deletion_reads_metadata_inside_ingest_lock(engine):
    # A spy checks the ordering without timing-sensitive sleeps or model calls.
    class Lock:
        active = False
        def __enter__(self): self.active = True
        def __exit__(self, *args): self.active = False
    lock = Lock()
    engine._lock = lock
    def read_message(_):
        assert lock.active
        return None
    engine.store.message = read_message
    from tmcra_local.engine import LocalEngineError
    with pytest.raises(LocalEngineError, match='does not exist'):
        engine.delete_message('missing')


def test_existing_soft_delete_is_backfilled_on_reopen(tmp_path):
    path = tmp_path/'local.sqlite3'
    store = LocalStore(path)
    register(store)
    with store.transaction(immediate=True) as db:
        db.execute("UPDATE messages SET deleted_at='before-upgrade'")
        db.execute('DROP TABLE source_tombstones')
    reopened = LocalStore(path)
    with pytest.raises(ValueError, match='permanently revoked'):
        register(reopened, message_id='new-id')
