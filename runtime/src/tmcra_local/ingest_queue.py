"""Durable admission, single-writer leasing and bounded asynchronous processing.

No model implementation lives here. Accepted sources and jobs share LocalStore's
SQLite transaction. A crash leaves running work interrupted, never silently
reissues an unknown model call. Explicit retry keeps the source identity.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path

from .storage import utc_now


class QueueError(ValueError):
    pass


class SourceChanged(RuntimeError):
    """A deletion invalidated the in-flight model result, never a model success."""
    def __init__(self, message, *, source_deleted=False):
        super().__init__(message)
        self.source_deleted = source_deleted


class InferenceLock:
    """RLock with explicit, portable, same-thread suspension at model boundary."""
    def __init__(self):
        self._lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self):
        self._lock.acquire()
        self._local.depth = getattr(self._local, 'depth', 0) + 1
        return self

    def __exit__(self, *_):
        self._local.depth -= 1
        self._lock.release()

    @contextmanager
    def suspended(self):
        depth = getattr(self._local, 'depth', 0)
        for _ in range(depth):
            self.__exit__()
        try:
            yield
        finally:
            for _ in range(depth):
                self.__enter__()


class ProcessWriterLease:
    """Serializes this installation's writers across threads AND processes."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._local = threading.local()

    @staticmethod
    def _lock_windows(handle, *, blocking):
        import msvcrt
        # LK_LOCK itself gives up after roughly ten seconds. Model calls may
        # take longer, so implement true blocking acquisition using NBLCK.
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if not blocking:
                    return False
                time.sleep(.025)

    @contextmanager
    def acquire(self, *, blocking=True):
        acquired = self._lock.acquire(blocking=blocking)
        if not acquired:
            yield False
            return
        handle = None
        try:
            if getattr(self._local, 'depth', 0):
                self._local.depth += 1
                try:
                    yield True
                finally:
                    self._local.depth -= 1
                return
            handle = self.path.open('a+b')
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                if not self._lock_windows(handle, blocking=blocking):
                    yield False
                    return
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError:
                    yield False
                    return
            self._local.depth = 1
            try:
                yield True
            finally:
                self._local.depth = 0
                if os.name == 'nt':
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            if handle:
                handle.close()
            self._lock.release()


def classify_failure(exc):
    if isinstance(exc, SourceChanged):
        return ('cancelled', 'source_deleted') if exc.source_deleted else ('interrupted', 'context_changed')
    status = getattr(exc, 'code', None) or (getattr(exc, 'metadata', {}) or {}).get('http_status')
    if status in {408, 429, 500, 502, 503, 504} or isinstance(exc, (TimeoutError, ConnectionError)):
        return 'retryable_failed', 'transport_temporary'
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        return 'retryable_failed', 'transport_temporary'
    # Do not store exception text: providers can echo raw source or credentials.
    if status and 400 <= int(status) < 500:
        return 'permanent_failed', 'request_rejected'
    if isinstance(exc, ValueError) or exc.__class__.__name__ in {'ProductWriterError', 'LocalEngineError'}:
        return 'permanent_failed', 'validation_failed'
    return 'interrupted', 'outcome_unknown'


class DurableIngestQueue:
    def __init__(self, store):
        self.store = store
        with store.transaction(immediate=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ingest_jobs (
                job_id TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL,
                scope_ids_json TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS ingest_jobs_status ON ingest_jobs(status,created_at,job_id)')
            db.execute('''CREATE TABLE IF NOT EXISTS ingest_job_attempts (
                job_id TEXT NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, ended_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(job_id,attempt))''')

    @staticmethod
    def _view(row):
        if row is None:
            return None
        row = dict(row)
        row.pop('scope_ids_json', None)
        row.update(accepted=True, indexed=row['status'] == 'indexed',
                   retryable=row['status'] in {'retryable_failed', 'interrupted'},
                   requires_explicit_retry=row['status'] in {'retryable_failed', 'interrupted', 'permanent_failed'})
        return row

    def admission(self, scope_ids):
        scopes = json.dumps(sorted(scope_ids), separators=(',', ':'))
        def attach(db, message, _created):
            identifier = message['message_id']
            existing = db.execute('SELECT * FROM ingest_jobs WHERE message_id=?', (identifier,)).fetchone()
            if existing:
                if existing['scope_ids_json'] != scopes:
                    raise QueueError('message already queued with different visibility')
                return
            statuses = {row['scope_id']: row['status'] for row in db.execute(
                'SELECT scope_id,status FROM message_scopes WHERE message_id=?', (identifier,))}
            complete = all(statuses.get(s) == 'committed' for s in scope_ids)
            now = utc_now()
            db.execute('INSERT INTO ingest_jobs VALUES(?,?,?,?,0,?,?,?)',
                       ('job_' + hashlib.sha256(identifier.encode()).hexdigest()[:32], identifier, scopes,
                        'indexed' if complete else 'queued', '', now, now))
            for scope in scope_ids:
                db.execute('INSERT OR IGNORE INTO message_scopes VALUES(?,?,?,?,?,?)',
                           (identifier, scope, 'pending', '', '', now))
        return attach

    def for_message(self, identifier):
        with self.store.transaction() as db:
            return self._view(db.execute('SELECT * FROM ingest_jobs WHERE message_id=?', (identifier,)).fetchone())

    def get(self, job_id):
        with self.store.transaction() as db:
            return self._view(db.execute('SELECT * FROM ingest_jobs WHERE job_id=?', (job_id,)).fetchone())

    def recover_interrupted(self):
        # Caller must hold exclusive ProcessWriterLease; never interrupt a live worker.
        with self.store.transaction(immediate=True) as db:
            db.execute("UPDATE ingest_job_attempts SET status='interrupted',error_code='process_interrupted',ended_at=? WHERE status='running'", (utc_now(),))
            return db.execute("UPDATE ingest_jobs SET status='interrupted',error_code='process_interrupted',updated_at=? WHERE status='running'", (utc_now(),)).rowcount

    def claim(self):
        with self.store.transaction(immediate=True) as db:
            row = db.execute("SELECT * FROM ingest_jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE ingest_jobs SET status='running',attempts=attempts+1,error_code='',updated_at=? WHERE job_id=?", (utc_now(), row['job_id']))
            db.execute('INSERT INTO ingest_job_attempts(job_id,attempt,status,started_at) VALUES(?,?,?,?)', (row['job_id'], row['attempts']+1, 'running', utc_now()))
            return dict(row)

    def finish(self, message_id, status, error_code=''):
        with self.store.transaction(immediate=True) as db:
            db.execute("UPDATE ingest_job_attempts SET status=?,error_code=?,ended_at=? WHERE status='running' AND job_id IN (SELECT job_id FROM ingest_jobs WHERE message_id=?)",
                       (status, error_code, utc_now(), message_id))
            # Deletion wins even if a late completion is delivered.
            db.execute("UPDATE ingest_jobs SET status=?,error_code=?,updated_at=? WHERE message_id=? AND status!='cancelled'",
                       (status, error_code, utc_now(), message_id))

    def retry(self, job_id, *, confirmed):
        if confirmed is not True:
            raise QueueError('explicit confirmation is required for retry')
        with self.store.transaction(immediate=True) as db:
            row = db.execute('SELECT * FROM ingest_jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is None:
                raise QueueError('job does not exist')
            if row['status'] in {'queued', 'running', 'indexed'}:
                return self._view(row)
            if row['status'] == 'cancelled' or row['attempts'] >= 3:
                raise QueueError('job is revoked or retry budget is exhausted')
            message = db.execute('SELECT deleted_at FROM messages WHERE message_id=?', (row['message_id'],)).fetchone()
            if message is None or message['deleted_at']:
                raise QueueError('source was deleted')
            db.execute("UPDATE ingest_jobs SET status='queued',error_code='',updated_at=? WHERE job_id=?", (utc_now(), job_id))
        return self.get(job_id)
