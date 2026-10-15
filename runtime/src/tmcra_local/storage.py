from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, *, maximum: int = 0) -> str:
    result = str(value or "").strip()
    if maximum and len(result) > maximum:
        raise ValueError(f"value exceeds {maximum} characters")
    if any(character in result for character in "\0\r\n"):
        raise ValueError("identifier contains a control character")
    return result


def _revocation_keys(message: Mapping[str, Any]) -> tuple[str, str]:
    """Match the two existing owner-local uniqueness constraints, without content."""
    identities = (
        ["message", str(message["message_id"])],
        ["native", str(message["source_app"]), str(message["native_thread_id"]),
         str(message["native_message_id"])],
    )
    return tuple(hashlib.sha256(
        ("tmcra.local.source-revocation.1\0" + _json(identity)).encode("utf-8")
    ).hexdigest() for identity in identities)


def _revoke_sources(connection: sqlite3.Connection, messages: Iterable[Mapping[str, Any]]) -> None:
    now = utc_now()
    for message in messages:
        connection.executemany(
            "INSERT OR IGNORE INTO source_tombstones(identity_sha256,revoked_at) VALUES (?,?)",
            ((key, now) for key in _revocation_keys(message)),
        )


class LocalStore:
    """Owner-local metadata and usage ledger.

    TMCRA graph records remain in the graph database owned by the algorithm
    adapter. This database tracks projects, sessions, immutable source-message
    identities, local processing state, knowledge projections, and provider
    usage. It contains no account, subscription, or server-side billing tables.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterable[sqlite3.Connection]:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize(self) -> None:
        with self.transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    domain_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    source_app TEXT NOT NULL,
                    native_thread_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_project_updated
                    ON sessions(project_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS local_runtime_meta (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO local_runtime_meta VALUES('privacy_revision',0);
                CREATE TABLE IF NOT EXISTS source_tombstones (
                    identity_sha256 TEXT PRIMARY KEY,
                    revoked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    message_index INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','system','tool')),
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    source_app TEXT NOT NULL,
                    native_thread_id TEXT NOT NULL,
                    native_message_id TEXT NOT NULL,
                    actor_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    deleted_at TEXT,
                    UNIQUE(session_id, message_index),
                    UNIQUE(source_app, native_thread_id, native_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_project_session
                    ON messages(project_id, session_id, message_index);
                CREATE TABLE IF NOT EXISTS message_scopes (
                    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
                    scope_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','processing','committed','failed','deleted')),
                    source_record_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(message_id, scope_id)
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    usage_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    task TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    prompt_cache_hit_tokens INTEGER NOT NULL,
                    prompt_cache_miss_tokens INTEGER NOT NULL,
                    latency_seconds REAL NOT NULL,
                    usage_reported INTEGER NOT NULL,
                    physical_call_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_physical_call
                    ON usage_events(physical_call_id) WHERE physical_call_id <> '';
                CREATE INDEX IF NOT EXISTS idx_usage_created
                    ON usage_events(created_at DESC);
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    scope_id TEXT PRIMARY KEY,
                    source_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generator TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            _revoke_sources(connection, connection.execute(
                "SELECT message_id,source_app,native_thread_id,native_message_id "
                "FROM messages WHERE deleted_at IS NOT NULL"
            ).fetchall())

            columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            if 'identity_metadata_json' not in columns:
                connection.execute("ALTER TABLE messages ADD COLUMN identity_metadata_json TEXT NOT NULL DEFAULT '{}'")

    def register_message(
        self,
        *,
        project_id: str,
        project_title: str,
        session_id: str,
        session_title: str,
        role: str,
        content: str,
        occurred_at: str,
        source_app: str,
        native_thread_id: str,
        native_message_id: str,
        actor: Mapping[str, Any],
        message_id: str = "",
        _after_register=None,
        identity_metadata=None,
    ) -> tuple[dict[str, Any], bool]:
        project_id = _text(project_id, maximum=256)
        session_id = _text(session_id, maximum=512)
        role = _text(role, maximum=32).lower()
        content = str(content or "")
        source_app = _text(source_app or "unknown", maximum=80).lower()
        native_thread_id = _text(native_thread_id or session_id, maximum=512)
        native_message_id = _text(native_message_id, maximum=512)
        if not project_id or not session_id or role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("project_id, session_id, and a valid role are required")
        if not content.strip():
            raise ValueError("message content cannot be empty")
        if not native_message_id:
            native_message_id = hashlib.sha256(
                f"{source_app}\0{native_thread_id}\0{role}\0{content}".encode("utf-8")
            ).hexdigest()[:32]
        stable_id = _text(message_id, maximum=512) or "msg_" + hashlib.sha256(
            f"{source_app}\0{native_thread_id}\0{native_message_id}".encode("utf-8")
        ).hexdigest()[:32]
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        from .core.entity_identity import normalize_identity_metadata
        identity_json = _json(normalize_identity_metadata(identity_metadata))
        now = utc_now()
        actor_payload = {
            str(key): str(value)
            for key, value in dict(actor or {}).items()
            if str(key).strip() and str(value).strip()
        }
        with self.transaction(immediate=True) as connection:
            keys = _revocation_keys({
                "message_id": stable_id, "source_app": source_app,
                "native_thread_id": native_thread_id, "native_message_id": native_message_id,
            })
            if connection.execute(
                "SELECT 1 FROM source_tombstones WHERE identity_sha256 IN (?,?)", keys
            ).fetchone() is not None:
                raise ValueError("message identity was permanently revoked by content deletion")
            existing = connection.execute(
                "SELECT * FROM messages WHERE message_id=? OR "
                "(source_app=? AND native_thread_id=? AND native_message_id=?)",
                (stable_id, source_app, native_thread_id, native_message_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["content_sha256"]) != content_hash
                    or str(existing["role"]) != role
                    or str(existing["project_id"]) != project_id
                    or str(existing["session_id"]) != session_id
                    or str(existing["identity_metadata_json"]) != identity_json
                ):
                    raise ValueError("message identity was already used for different content")
                if str(existing["actor_json"]) != _json(actor_payload):
                    # A replay may neither promote an unknown source to human
                    # nor replace any producer/capture declaration in place.
                    raise ValueError("message identity was already used with different actor provenance")
                if _after_register is not None:
                    _after_register(connection, dict(existing), False)
                return dict(existing), False
            existing_session = connection.execute(
                "SELECT project_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing_session is not None and str(existing_session["project_id"]) != project_id:
                raise ValueError("session_id is already bound to a different project")
            connection.execute(
                "INSERT INTO projects(project_id,title,domain_key,created_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                "title=excluded.title,updated_at=excluded.updated_at",
                (
                    project_id,
                    str(project_title or project_id).strip()[:200],
                    project_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO sessions(session_id,project_id,title,source_app,native_thread_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "title=excluded.title,source_app=excluded.source_app,"
                "native_thread_id=excluded.native_thread_id,updated_at=excluded.updated_at",
                (
                    session_id,
                    project_id,
                    str(session_title or session_id).strip()[:200],
                    source_app,
                    native_thread_id,
                    now,
                    now,
                ),
            )
            index_row = connection.execute(
                "SELECT COALESCE(MAX(message_index),-1)+1 AS next_index "
                "FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
            message_index = int(index_row["next_index"])
            connection.execute(
                "INSERT INTO messages(message_id,project_id,session_id,message_index,role,content,"
                "content_sha256,occurred_at,source_app,native_thread_id,native_message_id,actor_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    stable_id,
                    project_id,
                    session_id,
                    message_index,
                    role,
                    content,
                    content_hash,
                    str(occurred_at or now),
                    source_app,
                    native_thread_id,
                    native_message_id,
                    _json(actor_payload),
                    now,
                ),
            )
            connection.execute("UPDATE messages SET identity_metadata_json=? WHERE message_id=?", (identity_json, stable_id))
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id=?", (stable_id,)
            ).fetchone()
            if _after_register is not None:
                _after_register(connection, dict(row), True)
            return dict(row), True

    def message(self, message_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id=?", (message_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["actor"] = json.loads(str(result.pop("actor_json") or "{}"))
        result["identity_metadata"] = json.loads(result.pop("identity_metadata_json", "{}"))
        return result

    def messages_for_session(self, session_id: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE session_id=? "
                + ("" if include_deleted else "AND deleted_at IS NULL ")
                + "ORDER BY message_index",
                (session_id,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["actor"] = json.loads(str(item.pop("actor_json") or "{}"))
            item["identity_metadata"] = json.loads(item.pop("identity_metadata_json", "{}"))
            results.append(item)
        return results

    def messages(
        self,
        *,
        project_id: str = "",
        session_id: str = "",
        limit: int = 200,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """List owner-local source messages so users can inspect and delete them."""

        clauses = ["1=1" if include_deleted else "deleted_at IS NULL"]
        values: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            values.append(_text(project_id, maximum=256))
        if session_id:
            clauses.append("session_id=?")
            values.append(_text(session_id, maximum=512))
        bounded_limit = max(1, min(int(limit), 500))
        sql = (
            "SELECT * FROM messages WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at DESC,message_index DESC LIMIT ?"
        )
        values.append(bounded_limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, tuple(values)).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["actor"] = json.loads(str(item.pop("actor_json") or "{}"))
            item["identity_metadata"] = json.loads(item.pop("identity_metadata_json", "{}"))
            results.append(item)
        return results

    def sessions(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.*,COUNT(m.message_id) AS message_count "
            "FROM sessions s LEFT JOIN messages m ON m.session_id=s.session_id "
            "AND m.deleted_at IS NULL"
        )
        values: tuple[Any, ...] = ()
        if project_id:
            sql += " WHERE s.project_id=?"
            values = (project_id,)
        sql += " GROUP BY s.session_id ORDER BY s.updated_at DESC"
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(sql, values).fetchall()]

    def projects(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT p.*,COUNT(DISTINCT s.session_id) AS session_count,"
                "COUNT(m.message_id) AS message_count FROM projects p "
                "LEFT JOIN sessions s ON s.project_id=p.project_id "
                "LEFT JOIN messages m ON m.project_id=p.project_id AND m.deleted_at IS NULL "
                "GROUP BY p.project_id ORDER BY p.updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def scope_status(self, message_id: str, scope_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM message_scopes WHERE message_id=? AND scope_id=?",
                (message_id, scope_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def scopes_for_message(self, message_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM message_scopes WHERE message_id=? ORDER BY scope_id",
                (message_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_scope_status(
        self,
        *,
        message_id: str,
        scope_id: str,
        status: str,
        source_record_id: str = "",
        error: str = "",
    ) -> None:
        if status not in {"pending", "processing", "committed", "failed", "deleted"}:
            raise ValueError("invalid message scope status")
        with self.transaction(immediate=True) as connection:
            active = connection.execute("SELECT deleted_at FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if active is None or (active["deleted_at"] and status != 'deleted'):
                return
            connection.execute(
                "INSERT INTO message_scopes(message_id,scope_id,status,source_record_id,error,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(message_id,scope_id) DO UPDATE SET "
                "status=excluded.status,source_record_id=excluded.source_record_id,"
                "error=excluded.error,updated_at=excluded.updated_at",
                (message_id, scope_id, status, source_record_id, error[:2000], utc_now()),
            )

    def record_usage(
        self,
        metadata: Mapping[str, Any],
        *,
        project_id: str = "",
        session_id: str = "",
    ) -> None:
        call_id = str(metadata.get("physical_call_id") or "").strip()
        usage_id = "usage_" + (
            hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:32]
            if call_id
            else uuid.uuid4().hex
        )

        def integer(name: str) -> int:
            value = metadata.get(name, 0)
            return max(0, int(value)) if isinstance(value, (int, float)) else 0

        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO usage_events(usage_id,project_id,session_id,task,provider,model,"
                "prompt_tokens,completion_tokens,total_tokens,prompt_cache_hit_tokens,"
                "prompt_cache_miss_tokens,latency_seconds,usage_reported,physical_call_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    usage_id,
                    project_id,
                    session_id,
                    str(metadata.get("stage") or "generation")[:80],
                    str(metadata.get("provider") or "openai-compatible")[:80],
                    str(metadata.get("model") or "")[:200],
                    integer("prompt_tokens"),
                    integer("completion_tokens"),
                    integer("total_tokens"),
                    integer("prompt_cache_hit_tokens"),
                    integer("prompt_cache_miss_tokens"),
                    float(metadata.get("latency_seconds") or 0.0),
                    int(bool(metadata.get("usage_reported"))),
                    call_id,
                    utc_now(),
                ),
            )

    def usage_summary(self, *, project_id: str = "", limit: int = 50) -> dict[str, Any]:
        where = " WHERE project_id=?" if project_id else ""
        values: tuple[Any, ...] = (project_id,) if project_id else ()
        with closing(self._connect()) as connection:
            totals = connection.execute(
                "SELECT COUNT(*) AS calls,COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
                "COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
                "COALESCE(SUM(total_tokens),0) AS total_tokens,"
                "COALESCE(SUM(prompt_cache_hit_tokens),0) AS cache_hit_tokens,"
                "COALESCE(SUM(prompt_cache_miss_tokens),0) AS cache_miss_tokens "
                "FROM usage_events" + where,
                values,
            ).fetchone()
            recent = connection.execute(
                "SELECT usage_id,project_id,session_id,task,provider,model,prompt_tokens,"
                "completion_tokens,total_tokens,latency_seconds,usage_reported,created_at "
                "FROM usage_events" + where + " ORDER BY created_at DESC LIMIT ?",
                (*values, max(1, min(int(limit), 500))),
            ).fetchall()
        return {
            "billing_mode": "provider-direct-or-local",
            "tmcra_charge": 0,
            "currency": None,
            "totals": dict(totals),
            "recent": [dict(row) for row in recent],
        }

    def save_knowledge(
        self,
        *,
        scope_id: str,
        source_fingerprint: str,
        payload: Mapping[str, Any],
        generator: str,
    ) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO knowledge_documents(scope_id,source_fingerprint,payload_json,generator,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET "
                "source_fingerprint=excluded.source_fingerprint,payload_json=excluded.payload_json,"
                "generator=excluded.generator,updated_at=excluded.updated_at",
                (scope_id, source_fingerprint, _json(payload), generator, now, now),
            )

    def knowledge(self, scope_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_documents WHERE scope_id=?", (scope_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(str(result.pop("payload_json")))
        return result

    def privacy_revision(self) -> int:
        with self.transaction() as connection:
            return int(connection.execute("SELECT value FROM local_runtime_meta WHERE key='privacy_revision'").fetchone()[0])

    def mark_message_deleted(self, message_id: str) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute("UPDATE local_runtime_meta SET value=value+1 WHERE key='privacy_revision'")
            _revoke_sources(connection, connection.execute(
                "SELECT message_id,source_app,native_thread_id,native_message_id "
                "FROM messages WHERE message_id=?", (message_id,)
            ).fetchall())
            connection.execute(
                "UPDATE messages SET deleted_at=? WHERE message_id=?", (now, message_id)
            )
            connection.execute(
                "UPDATE message_scopes SET status='deleted',updated_at=? WHERE message_id=?",
                (now, message_id),
            )

    def erase_message_metadata(self, message_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            _revoke_sources(connection, connection.execute(
                "SELECT message_id,source_app,native_thread_id,native_message_id "
                "FROM messages WHERE message_id=?", (message_id,)
            ).fetchall())
            deleted = connection.execute(
                "DELETE FROM messages WHERE message_id=?", (message_id,)
            ).rowcount
        return bool(deleted)

    def invalidate_knowledge(self, scope_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM knowledge_documents WHERE scope_id=?", (scope_id,)
            )

    def delete_project_metadata(self, project_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            _revoke_sources(connection, connection.execute(
                "SELECT message_id,source_app,native_thread_id,native_message_id "
                "FROM messages WHERE project_id=?", (project_id,)
            ).fetchall())
            connection.execute(
                "DELETE FROM knowledge_documents WHERE scope_id=?",
                ("project:" + project_id,),
            )
            connection.execute("DELETE FROM usage_events WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM projects WHERE project_id=?", (project_id,))

    def secure_compact(self) -> None:
        """Truncate the WAL and rewrite free pages after an explicit deletion.

        This makes the local databases honor a user's delete request at the
        SQLite layer. It cannot remove copies held by external backups,
        snapshots, filesystem journals, or a model provider.
        """

        with self._lock, closing(self._connect()) as connection:
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


__all__ = ["LocalStore", "utc_now"]
