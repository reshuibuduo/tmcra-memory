"""Project workflows with revisioned evidence and explicit execution assurance.

This module never executes commands, fetches URLs or reads evidence paths. Source
IDs are resolved by a caller-provided, namespace-bound resolver. Statements from
agents, users, tools and imported packages remain reports, not execution proof.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "tmcra.workflows.1"
KINDS = ("task", "attempt", "decision", "reminder")
EVENTS = {"project_open", "question", "task_switch"}
MAX_RECORDS = 1000
_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,200}$")
_EXECUTION_CAPABILITY = object()


class WorkflowError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("invalid_json", "Only finite JSON values are supported") from exc


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise WorkflowError("invalid_field", f"{name} must be a bounded identifier")
    return value


def _text(value: Any, name: str, maximum: int = 4000, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise WorkflowError("invalid_field", f"{name} must be text of at most {maximum} characters")
    return value.strip()


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise WorkflowError("invalid_field", f"{name} must be an integer between {low} and {high}")
    return value


def _strings(value: Any, name: str, *, ids: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 50:
        raise WorkflowError("invalid_field", f"{name} must be a list with at most 50 entries")
    return list(dict.fromkeys(_identifier(x, name) if ids else _text(x, name, 1000, empty=False) for x in value))


def _time(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timezone required")
        return stamp.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise WorkflowError("invalid_time", "Timestamp must be ISO 8601 with timezone") from exc


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class WorkflowStore:
    def __init__(self, path: str | Path, source_resolver: Callable[[str, str], Mapping[str, Any] | None] | None = None):
        self.path = str(path)
        self.source_resolver = source_resolver
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflow_namespaces(namespace TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, recorded_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workflow_records(namespace TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL, body TEXT, deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(namespace,kind,id));
                CREATE TABLE IF NOT EXISTS workflow_events(namespace TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, recorded_at TEXT NOT NULL, body TEXT, PRIMARY KEY(namespace,revision,kind,id));
                CREATE TABLE IF NOT EXISTS workflow_operations(namespace TEXT NOT NULL, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(namespace,operation_id));
                CREATE TABLE IF NOT EXISTS workflow_command_claims(namespace TEXT NOT NULL, operation_id TEXT NOT NULL, PRIMARY KEY(namespace,operation_id));
                CREATE TABLE IF NOT EXISTS workflow_revoked_sources(namespace TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(namespace,source_id));
                CREATE INDEX IF NOT EXISTS workflow_events_time ON workflow_events(namespace,recorded_at);
            """)

        Path(self.path).chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _state(self, db, namespace):
        return {(r["kind"], r["id"]): json.loads(r["body"]) for r in db.execute("SELECT * FROM workflow_records WHERE namespace=? AND deleted=0", (namespace,))}

    def _sources(self, db, namespace, value):
        ids = _strings(value, "evidence_ids", ids=True)
        for source_id in ids:
            if db.execute("SELECT 1 FROM workflow_revoked_sources WHERE namespace=? AND source_id=?", (namespace, source_id)).fetchone():
                raise WorkflowError("source_revoked", "Evidence source has been deleted", 409)
            if self.source_resolver is not None and self.source_resolver(namespace, source_id) is None:
                raise WorkflowError("source_not_found", "Evidence source does not exist in this project", 404)
        return ids

    def _allowed(self, payload, fields):
        extra = set(payload) - set(fields) - {"operation_id", "expected_revision"}
        if extra:
            raise WorkflowError("unsupported_field", f"Unsupported fields: {', '.join(sorted(extra))}")

    def _get(self, state, kind, ident):
        item = state.get((kind, ident))
        if item is None:
            raise WorkflowError("record_not_found", f"{kind} record does not exist", 404)
        return item

    def _new(self, db, namespace, state, kind, ident):
        if (kind, ident) in state:
            raise WorkflowError("record_exists", "Record already exists", 409)
        if db.execute("SELECT 1 FROM workflow_records WHERE namespace=? AND kind=? AND id=? AND deleted=1", (namespace, kind, ident)).fetchone():
            raise WorkflowError("record_deleted", "Deleted record IDs cannot be replayed", 409)
        if len(state) >= MAX_RECORDS:
            raise WorkflowError("record_limit", "Project workflow record limit reached", 409)

    def _dependencies(self, state, ident, values):
        deps = _strings(values, "depends_on", ids=True)
        for dep in deps:
            self._get(state, "decision", dep)
            pending, visited = [dep], set()
            while pending:
                node = pending.pop()
                if node == ident:
                    raise WorkflowError("dependency_cycle", "Decision dependencies must be acyclic")
                if node not in visited:
                    visited.add(node)
                    pending.extend(state[("decision", node)].get("depends_on", []))
        return deps

    def execute(self, namespace: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._execute(namespace, action, payload)

    def _execute(self, namespace, action, payload, *, capability=None):
        namespace = _identifier(namespace, "namespace")
        if not isinstance(payload, Mapping) or len(_json(payload).encode()) > 65536:
            raise WorkflowError("invalid_payload", "Payload must be a JSON object of at most 64 KiB")
        payload = dict(payload)
        operation = _identifier(payload.get("operation_id"), "operation_id")
        digest = _fingerprint({"action": action, "payload": payload, "local_execution": capability is _EXECUTION_CAPABILITY})
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if capability is not _EXECUTION_CAPABILITY and db.execute("SELECT 1 FROM workflow_command_claims WHERE namespace=? AND operation_id=?", (namespace, operation)).fetchone():
                raise WorkflowError("operation_reserved", "Operation ID is reserved for local execution", 409)
            oldop = db.execute("SELECT * FROM workflow_operations WHERE namespace=? AND operation_id=?", (namespace, operation)).fetchone()
            if oldop:
                if oldop["fingerprint"] != digest:
                    raise WorkflowError("operation_conflict", "operation_id was already used with different content", 409)
                return json.loads(oldop["result"])
            nrow = db.execute("SELECT * FROM workflow_namespaces WHERE namespace=?", (namespace,)).fetchone()
            current = nrow["revision"] if nrow else 0
            expected = payload.get("expected_revision")
            if expected is not None and _integer(expected, "expected_revision", 0, 2**63-2) != current:
                raise WorkflowError("revision_conflict", "Project revision changed; reload before editing", 409)
            stamp = self._now()
            if nrow and stamp <= _time(nrow["recorded_at"]):
                stamp = _time(nrow["recorded_at"]) + timedelta(microseconds=1)
            now = _iso(stamp)
            revision = current + 1
            state = self._state(db, namespace)
            before = {key: _json(value) for key, value in state.items()}
            deleted = []
            result = {"ok": True, "action": action, "revision": revision, "recorded_at": now}
            ident = None
            if action == "task.upsert":
                self._allowed(payload, {"id", "title", "goal", "next_step", "status", "version", "evidence_ids", "stale_after_seconds"})
                ident = _identifier(payload.get("id"), "id")
                old = state.get(("task", ident))
                if old is None:
                    self._new(db, namespace, state, "task", ident)
                    old = {"kind": "task", "id": ident, "receipts": []}
                body = dict(old)
                for field in ("title", "goal", "next_step", "version"):
                    body[field] = _text(payload.get(field, old.get(field, "")), field, 4000 if field != "version" else 256)
                if not body["title"] and not body["goal"]:
                    raise WorkflowError("invalid_field", "A task title or goal is required")
                if any(body.get(field) != old.get(field) for field in ("title", "goal", "version")):
                    body["definition_revision"] = revision
                else:
                    body["definition_revision"] = old.get("definition_revision", revision)
                body["status"] = payload.get("status", old.get("status", "todo"))
                if body["status"] not in {"todo", "in_progress", "blocked", "done"}:
                    raise WorkflowError("invalid_field", "Unknown task status")
                body["evidence_ids"] = self._sources(db, namespace, payload.get("evidence_ids", old.get("evidence_ids", [])))
                body["stale_after_seconds"] = _integer(payload.get("stale_after_seconds", old.get("stale_after_seconds", 86400)), "stale_after_seconds", 1, 2592000)
                state[("task", ident)] = body
            elif action in {"task.receipt", "_task.executed"}:
                private = action == "_task.executed" and capability is _EXECUTION_CAPABILITY
                if action == "_task.executed" and not private:
                    raise WorkflowError("unsupported_action", "Unknown workflow action")
                allowed = {"id", "summary", "evidence_ids", "outcome", "version"}
                if private:
                    allowed |= {"exit_code", "stdout_sha256", "stderr_sha256", "started_at", "finished_at", "timed_out", "command_kind", "task_revision"}
                self._allowed(payload, allowed)
                ident = _identifier(payload.get("id"), "id")
                body = dict(self._get(state, "task", ident))
                if len(body["receipts"]) >= 100:
                    raise WorkflowError("receipt_limit", "Task receipt limit reached", 409)
                outcome = payload.get("outcome", "unknown")
                if outcome not in {"passed", "failed", "unknown"}:
                    raise WorkflowError("invalid_field", "Unknown outcome")
                receipt = {"id": operation, "assurance": "executed_local" if private else "reported", "outcome": outcome,
                           "summary": _text(payload.get("summary", ""), "summary"), "version": _text(payload.get("version", ""), "version", 256),
                           "evidence_ids": self._sources(db, namespace, payload.get("evidence_ids", [])), "recorded_at": now}
                if private:
                    receipt.update({key: payload[key] for key in allowed - {"id", "summary", "evidence_ids", "outcome", "version"}})
                    _integer(receipt["exit_code"], "exit_code", -2147483648, 2147483647)
                    for key in ("stdout_sha256", "stderr_sha256"):
                        if not re.fullmatch("[0-9a-f]{64}", receipt[key]):
                            raise WorkflowError("invalid_field", "Execution digest must be SHA256")
                    if _time(receipt["started_at"]) > _time(receipt["finished_at"]) or _time(receipt["finished_at"]) > stamp + timedelta(seconds=1):
                        raise WorkflowError("invalid_time", "Execution timestamps are inconsistent")
                    if type(receipt["timed_out"]) is not bool:
                        raise WorkflowError("invalid_field", "timed_out must be boolean")
                body["receipts"] = [*body["receipts"], receipt]
                state[("task", ident)] = body
            elif action == "attempt.record":
                self._allowed(payload, {"id", "task_id", "config", "outcome", "summary", "evidence_ids", "retry_conditions"})
                ident = _identifier(payload.get("id"), "id")
                self._new(db, namespace, state, "attempt", ident)
                task_id = _identifier(payload.get("task_id"), "task_id")
                self._get(state, "task", task_id)
                config = payload.get("config", {})
                if not isinstance(config, dict) or len(_json(config)) > 8192:
                    raise WorkflowError("invalid_field", "config must be a JSON object of at most 8192 characters")
                outcome = payload.get("outcome", "unknown")
                if outcome not in {"passed", "failed", "unknown"}:
                    raise WorkflowError("invalid_field", "Unknown outcome")
                state[("attempt", ident)] = {"kind": "attempt", "id": ident, "task_id": task_id, "config_fingerprint": _fingerprint(config),
                    "outcome": outcome, "summary": _text(payload.get("summary", ""), "summary"), "assurance": "reported",
                    "evidence_ids": self._sources(db, namespace, payload.get("evidence_ids", [])), "retry_conditions": _strings(payload.get("retry_conditions", []), "retry_conditions")}
                # Store only config fingerprint, not potentially secret configuration values.
            elif action in {"decision.create", "decision.revise"}:
                self._allowed(payload, {"id", "title", "text", "evidence_ids", "depends_on", "reason"})
                ident = _identifier(payload.get("id"), "id")
                if action == "decision.create":
                    self._new(db, namespace, state, "decision", ident)
                    old = {"kind": "decision", "id": ident, "title": ""}
                else:
                    old = self._get(state, "decision", ident)
                    _text(payload.get("reason"), "reason", empty=False)
                body = dict(old)
                body.update({"title": _text(payload.get("title", old.get("title", "")), "title", 500),
                    "text": _text(payload.get("text"), "text", empty=False), "reason": _text(payload.get("reason", ""), "reason"),
                    "evidence_ids": self._sources(db, namespace, payload.get("evidence_ids", [])),
                    "depends_on": self._dependencies(state, ident, payload.get("depends_on", old.get("depends_on", []))),
                    "needs_review": False, "review_reason": "", "assurance": "reported"})
                state[("decision", ident)] = body
                if action == "decision.revise":
                    affected, pending = set(), [ident]
                    while pending:
                        changed = pending.pop()
                        for (kind, key), decision in list(state.items()):
                            if kind == "decision" and key != ident and key not in affected and changed in decision.get("depends_on", []):
                                state[(kind, key)] = {**decision, "needs_review": True, "review_reason": f"Dependency {ident} was revised; original statement is unchanged"}
                                affected.add(key)
                                pending.append(key)
                    result["affected_decisions"] = sorted(affected)
            elif action == "reminder.upsert":
                self._allowed(payload, {"id", "title", "reason", "evidence_ids", "events", "task_id", "query_terms", "cooldown_seconds"})
                ident = _identifier(payload.get("id"), "id")
                old = state.get(("reminder", ident))
                if old is None:
                    self._new(db, namespace, state, "reminder", ident)
                    old = {"kind": "reminder", "id": ident, "state": "active", "last_shown_at": None, "snoozed_until": None}
                events = _strings(payload.get("events", ["project_open"]), "events")
                if not events or not set(events) <= EVENTS:
                    raise WorkflowError("invalid_event", "Reminders require project_open, question or task_switch")
                task_id = payload.get("task_id", old.get("task_id"))
                if task_id is not None:
                    self._get(state, "task", _identifier(task_id, "task_id"))
                state[("reminder", ident)] = {**old, "title": _text(payload.get("title", old.get("title", "")), "title", 500, empty=False),
                    "reason": _text(payload.get("reason", old.get("reason", "")), "reason", empty=False),
                    "evidence_ids": self._sources(db, namespace, payload.get("evidence_ids", old.get("evidence_ids", []))),
                    "events": events, "task_id": task_id, "query_terms": _strings(payload.get("query_terms", []), "query_terms"),
                    "cooldown_seconds": _integer(payload.get("cooldown_seconds", 86400), "cooldown_seconds", 1, 2592000)}
            elif action == "reminder.evaluate":
                self._allowed(payload, {"event", "question", "task_id", "config"})
                event = payload.get("event")
                if event not in EVENTS:
                    raise WorkflowError("invalid_event", "Only explicit project/question/task events trigger reminders")
                question = _text(payload.get("question", ""), "question", 8192).casefold()
                task_id = payload.get("task_id")
                if task_id is not None:
                    self._get(state, "task", _identifier(task_id, "task_id"))
                config = payload.get("config")
                if config is not None and (not isinstance(config, dict) or len(_json(config)) > 8192):
                    raise WorkflowError("invalid_field", "config must be a small JSON object")
                if task_id and config is not None:
                    for (kind, key), attempt in list(state.items()):
                        if kind == "attempt" and attempt["task_id"] == task_id and attempt["outcome"] == "failed" and attempt["config_fingerprint"] == _fingerprint(config):
                            rid = "failed." + hashlib.sha256(key.encode()).hexdigest()[:24]
                            if ("reminder", rid) not in state:
                                if db.execute("SELECT 1 FROM workflow_records WHERE namespace=? AND kind='reminder' AND id=? AND deleted=1", (namespace, rid)).fetchone():
                                    continue
                                self._new(db, namespace, state, "reminder", rid)
                                state[("reminder", rid)] = {"kind": "reminder", "id": rid, "title": "Previous failure with the same configuration", "reason": attempt["summary"],
                                    "evidence_ids": attempt["evidence_ids"], "events": sorted(EVENTS), "task_id": task_id, "query_terms": [], "state": "active",
                                    "last_shown_at": None, "snoozed_until": None, "cooldown_seconds": 86400, "generated_from": key, "config_fingerprint": attempt["config_fingerprint"],
                                    "retry_conditions": attempt["retry_conditions"], "blocks_retry": False}
                shown = []
                for (kind, key), reminder in sorted(state.items()):
                    if kind != "reminder" or reminder["state"] != "active" or event not in reminder["events"]:
                        continue
                    if reminder.get("task_id") and reminder["task_id"] != task_id:
                        continue
                    if reminder.get("config_fingerprint") and (config is None or reminder["config_fingerprint"] != _fingerprint(config)):
                        continue
                    if reminder["query_terms"] and not any(term.casefold() in question for term in reminder["query_terms"]):
                        continue
                    if reminder.get("snoozed_until") and _time(reminder["snoozed_until"]) > stamp:
                        continue
                    if reminder.get("last_shown_at") and (_time(reminder["last_shown_at"]) + timedelta(seconds=reminder["cooldown_seconds"])) > stamp:
                        continue
                    if self.source_resolver is not None and any(self.source_resolver(namespace, source) is None for source in reminder["evidence_ids"]):
                        continue
                    state[(kind, key)] = {**reminder, "last_shown_at": now}
                    shown.append({**state[(kind, key)], "why_now": f"{event}: {reminder['reason']}", "actions": ["complete", "ignore", "snooze"]})
                    if len(shown) == 3:
                        break
                result["reminders"] = shown
            elif action == "reminder.act":
                self._allowed(payload, {"id", "disposition", "snooze_seconds"})
                ident = _identifier(payload.get("id"), "id")
                reminder = dict(self._get(state, "reminder", ident))
                disposition = payload.get("disposition")
                if disposition in {"complete", "ignore"}:
                    reminder["state"] = "completed" if disposition == "complete" else "ignored"
                elif disposition == "snooze":
                    seconds = _integer(payload.get("snooze_seconds", 3600), "snooze_seconds", 1, 2592000)
                    reminder["snoozed_until"] = _iso(stamp + timedelta(seconds=seconds))
                    reminder["last_shown_at"] = None
                else:
                    raise WorkflowError("invalid_field", "Unknown reminder disposition")
                state[("reminder", ident)] = reminder
            elif action == "record.delete":
                self._allowed(payload, {"kind", "id"})
                kind = payload.get("kind")
                if kind not in KINDS:
                    raise WorkflowError("invalid_field", "Unknown record kind")
                ident = _identifier(payload.get("id"), "id")
                self._get(state, kind, ident)
                deleted = self._delete_closure(state, {(kind, ident)}, db.execute("SELECT body FROM workflow_events WHERE namespace=?", (namespace,)).fetchall())
                self._redact(db, namespace, deleted, revision, now)
                for key in deleted:
                    state.pop(key, None)
                result["deleted"] = [{"kind": k, "id": i} for k, i in sorted(deleted)]
            else:
                raise WorkflowError("unsupported_action", "Unknown workflow action")
            for key, body in state.items():
                if before.get(key) != _json(body):
                    body.update({"revision": revision, "recorded_at": now})
                    self._save(db, namespace, body)
            db.execute("INSERT INTO workflow_namespaces VALUES(?,?,?) ON CONFLICT(namespace) DO UPDATE SET revision=excluded.revision,recorded_at=excluded.recorded_at", (namespace, revision, now))
            if ident and action != "record.delete":
                kind = "task" if action == "_task.executed" else action.split(".")[0]
                result["record"] = state[(kind, ident)]
            db.execute("INSERT INTO workflow_operations VALUES(?,?,?,?)", (namespace, operation, digest, _json(result)))
            return result

    def _save(self, db, namespace, body):
        args = (namespace, body["kind"], body["id"], body["revision"], body["recorded_at"], _json(body))
        db.execute("INSERT INTO workflow_records VALUES(?,?,?,?,?,?,0) ON CONFLICT(namespace,kind,id) DO UPDATE SET revision=excluded.revision,recorded_at=excluded.recorded_at,body=excluded.body,deleted=0", args)
        db.execute("INSERT INTO workflow_events VALUES(?,?,?,?,?,?)", (namespace, body["revision"], body["kind"], body["id"], body["recorded_at"], _json(body)))

    def _delete_closure(self, state, initial, history=()):
        removed = set(initial)
        versions = list(state.items())
        for row in history:
            body = json.loads(row["body"])
            versions.append(((body["kind"], body["id"]), body))
        while True:
            more = {key for key, body in versions if (("task", body.get("task_id")) in removed or ("attempt", body.get("generated_from")) in removed or any(("decision", dep) in removed for dep in body.get("depends_on", [])))} - removed
            if not more:
                return removed
            removed.update(more)

    def _redact(self, db, namespace, removed, revision, now):
        for kind, ident in removed:
            db.execute("DELETE FROM workflow_events WHERE namespace=? AND kind=? AND id=?", (namespace, kind, ident))
            db.execute("INSERT INTO workflow_records VALUES(?,?,?,?,?,NULL,1) ON CONFLICT(namespace,kind,id) DO UPDATE SET body=NULL,deleted=1,revision=excluded.revision,recorded_at=excluded.recorded_at", (namespace, kind, ident, revision, now))
        # Operation receipts must not retain historical private bodies after deletion.
        for row in db.execute("SELECT operation_id,result FROM workflow_operations WHERE namespace=?", (namespace,)).fetchall():
            result = json.loads(row["result"])
            redacted = {k: result[k] for k in ("ok", "action", "revision", "recorded_at") if k in result}
            redacted["redacted_after_delete"] = True
            db.execute("UPDATE workflow_operations SET result=? WHERE namespace=? AND operation_id=?", (_json(redacted), namespace, row["operation_id"]))

    def revoke_sources(self, namespace: str, source_ids: list[str]) -> dict[str, Any]:
        namespace = _identifier(namespace, "namespace")
        if not isinstance(source_ids, list):
            raise WorkflowError("invalid_field", "source_ids must be a list")
        # Trusted internal deletion accepts the full session, not the 50-item
        # interactive evidence limit; every identifier remains validated.
        source_ids = [_identifier(value, "source_ids") for value in source_ids]
        return self._purge(namespace, set(source_ids), False)

    def purge_namespace(self, namespace: str) -> dict[str, Any]:
        return self._purge(_identifier(namespace, "namespace"), set(), True)

    def _purge(self, namespace, source_ids, all_records):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db, namespace)
            row = db.execute("SELECT revision,recorded_at FROM workflow_namespaces WHERE namespace=?", (namespace,)).fetchone()
            revision = (row["revision"] if row else 0) + 1
            stamp = max(self._now(), _time(row["recorded_at"]) + timedelta(microseconds=1)) if row else self._now()
            now = _iso(stamp)
            targets = set(state) if all_records else set()
            # Scan all versions: a source removed from the current text can still
            # be private in an earlier revision. Delete that entire record.
            for item in db.execute("SELECT kind,id,body FROM workflow_events WHERE namespace=?", (namespace,)):
                body = json.loads(item["body"])
                ids = set(body.get("evidence_ids", []))
                for receipt in body.get("receipts", []):
                    ids.update(receipt.get("evidence_ids", []))
                if all_records:
                    source_ids.update(ids)
                if ids & source_ids:
                    targets.add((item["kind"], item["id"]))
            # A decision derived from a revoked decision can repeat its private
            # content. Source revocation conservatively erases descendants too.
            while True:
                more = {key for key, body in state.items() if key[0] == "decision" and any(("decision", dep) in targets for dep in body.get("depends_on", []))} - targets
                if not more:
                    break
                targets.update(more)
            removed = self._delete_closure(state, targets, db.execute("SELECT body FROM workflow_events WHERE namespace=?", (namespace,)).fetchall())
            self._redact(db, namespace, removed, revision, now)
            db.executemany("INSERT OR IGNORE INTO workflow_revoked_sources VALUES(?,?)", ((namespace, source) for source in source_ids))
            db.execute("INSERT INTO workflow_namespaces VALUES(?,?,?) ON CONFLICT(namespace) DO UPDATE SET revision=excluded.revision,recorded_at=excluded.recorded_at", (namespace, revision, now))
            return {"ok": True, "revision": revision, "deleted_count": len(removed)}

    def snapshot(self, namespace: str, as_of: str | None = None) -> dict[str, Any]:
        namespace = _identifier(namespace, "namespace")
        cutoff = _time(as_of) if as_of is not None else self._now()
        with self._connect() as db:
            rows = db.execute("SELECT e.* FROM workflow_events e JOIN workflow_records r ON r.namespace=e.namespace AND r.kind=e.kind AND r.id=e.id WHERE e.namespace=? AND r.deleted=0 AND e.recorded_at<=? ORDER BY e.revision", (namespace, _iso(cutoff))).fetchall()
            latest, histories = {}, {}
            for row in rows:
                key = (row["kind"], row["id"])
                latest[key] = json.loads(row["body"])
                if row["kind"] == "decision":
                    histories.setdefault(key, []).append(json.loads(row["body"]))
            namespace_row = db.execute("SELECT * FROM workflow_namespaces WHERE namespace=?", (namespace,)).fetchone()
            revision = max((r["revision"] for r in rows), default=0) if as_of is not None else (namespace_row["revision"] if namespace_row else 0)
            result = {"schema_version": SCHEMA_VERSION, "namespace": namespace, "revision": revision, "recorded_at": _iso(cutoff), "tasks": [], "attempts": [], "decisions": [], "reminders": []}
            for key, body in sorted(latest.items()):
                missing = self.source_resolver is not None and any(self.source_resolver(namespace, source) is None for source in body.get("evidence_ids", []))
                body["evidence_available"] = not missing
                if key[0] == "task":
                    executed = [r for r in body["receipts"] if r["assurance"] == "executed_local"]
                    recent = executed[-1] if executed else None
                    receipt_missing = bool(recent and self.source_resolver is not None and any(self.source_resolver(namespace, source) is None for source in recent.get("evidence_ids", [])))
                    valid = bool(not receipt_missing and recent and recent.get("task_revision") == body.get("definition_revision") and recent["exit_code"] == 0 and not recent["timed_out"] and recent["version"] == body["version"] and body["version"] and (_time(recent["recorded_at"]) + timedelta(seconds=body["stale_after_seconds"])) >= cutoff)
                    body["verified"] = valid and not missing
                    body["verification_state"] = "executed" if body["verified"] else ("stale" if recent and recent["exit_code"] == 0 else ("execution_failed" if recent else ("reported" if body["receipts"] else "unverified")))
                    body["last_verified_at"] = recent["recorded_at"] if recent and recent["exit_code"] == 0 else None
                    body["verification_boundary"] = "Command execution only; not proof of claim correctness, deployment or external truth"
                elif key[0] == "decision":
                    body["history"] = histories[key]
                    if missing:
                        body["needs_review"] = True
                        body["review_reason"] = "A cited source was removed, suppressed or corrected; recheck this decision"
                    if any(("decision", dep) not in latest for dep in body["depends_on"]):
                        body["needs_review"] = True
                        body["review_reason"] = "A dependency was deleted or is unavailable in this historical view"
                result[key[0] + "s"].append(body)
            return result
