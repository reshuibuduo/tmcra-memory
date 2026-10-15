"""Explicit, owner-local memory packages: no network, paths, or code execution.

SHA256 detects accidental modification; it is NOT a signature or proof of origin.
``commit`` creates an import plan, not a search-index entry. The application must
ingest each pending record through its ordinary authenticated engine, retaining
the original source identity, then call ``mark_applied`` with the actual receipt.
Every imported assertion stays ``external_unverified`` regardless of its role.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

SCHEMA = "tmcra.memory-package.v1"
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 100
MAX_TEXT = 65536
MAX_EXPLICIT_RETRIES = 3
SENSITIVE_FIELDS = {"text", "source_identity", "provenance"}
_ITEM_FIELDS = {"id", "kind", "text", "source", "title", "role", "occurred_at",
                "provenance", "dependencies", "sensitive_fields", "verification", "item_sha256"}
_PACKAGE_FIELDS = {"schema_version", "package_id", "created_at", "source_namespace",
                   "mode", "items", "sha256"}


class BridgeError(ValueError):
    """A package or approval failed validation; nothing was imported."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BridgeError("package must contain bounded JSON data") from exc


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, maximum: int = 512, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise BridgeError(f"{field} must be a nonempty string")
    if len(value) > maximum or any(ord(c) < 32 for c in value):
        raise BridgeError(f"{field} is too long or contains control characters")
    return value


def _namespace(value: Any) -> str:
    return _text(value, "namespace", 256)


def _timestamp(value: Any, field: str, *, empty: bool = False) -> str:
    value = _text(value, field, 64, empty=empty)
    if not value and empty:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
    except ValueError as exc:
        raise BridgeError(f"{field} must be an ISO8601 time with timezone") from exc
    return value


def _ids(value: Any, field: str = "selected_ids", *, empty: bool = False, limit: int = MAX_ITEMS) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or (not value and not empty):
        raise BridgeError(f"{field} requires an explicit list of 1..{limit} identifiers")
    result = [_text(x, field) for x in value]
    if len(set(result)) != len(result):
        raise BridgeError(f"{field} contains duplicate identifiers")
    return result


def _source(value: Any, *, provenance: bool = False) -> dict[str, str]:
    fields = {"system", "namespace", "id"} | ({"role", "occurred_at"} if provenance else set())
    if not isinstance(value, dict) or set(value) - fields or not {"system", "namespace", "id"} <= set(value):
        raise BridgeError("source identity must contain system, namespace and id only")
    result = {"system": _text(value["system"], "source.system", 128),
              "namespace": _namespace(value["namespace"]), "id": _text(value["id"], "source.id")}
    if provenance:
        result["role"] = _text(value.get("role", "unknown"), "provenance.role", 32)
        result["occurred_at"] = _timestamp(value.get("occurred_at", ""), "provenance.occurred_at", empty=True)
    return result


def _identity(source: Mapping[str, str]) -> str:
    return content_sha256(["tmcra.bridge.identity.v1", source["system"], source["namespace"], source["id"]])


def _item(value: Any, *, exported: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _ITEM_FIELDS:
        raise BridgeError("unknown item fields; use the explicit package schema")
    required = _ITEM_FIELDS if exported else {"id", "kind", "text", "source"}
    if not required <= set(value):
        raise BridgeError("item is missing required fields")
    kind = value["kind"]
    if kind not in {"knowledge", "source", "revocation"}:
        raise BridgeError("kind must be knowledge, source or revocation")
    text = value["text"]
    if not isinstance(text, str) or (not text.strip() and kind != "revocation") or len(text) > MAX_TEXT or "\0" in text:
        raise BridgeError("text must be nonempty and at most 65536 characters")
    if kind == "revocation" and text:
        raise BridgeError("revocation records must not contain deleted source text")
    role = value.get("role", "knowledge" if kind == "knowledge" else "user")
    if role not in {"knowledge", "user", "assistant", "system", "tool"}:
        raise BridgeError("unsupported source role")
    provenance = value.get("provenance", [])
    if not isinstance(provenance, list) or len(provenance) > 64:
        raise BridgeError("provenance must be a list of at most 64 source references")
    sensitive = _ids(value.get("sensitive_fields", []), "sensitive_fields", empty=True, limit=3)
    if set(sensitive) - SENSITIVE_FIELDS:
        raise BridgeError("unsupported sensitive field")
    result = {
        "id": _text(value["id"], "id"), "kind": kind, "text": text,
        "source": _source(value["source"]), "title": _text(value.get("title", ""), "title", empty=True),
        "role": role, "occurred_at": _timestamp(value.get("occurred_at", ""), "occurred_at", empty=True),
        "provenance": [_source(p, provenance=True) for p in provenance],
        "dependencies": _ids(value.get("dependencies", []), "dependencies", empty=True, limit=64),
        "sensitive_fields": sorted(sensitive), "verification": "external_unverified",
    }
    if kind == "revocation" and (result["provenance"] or result["dependencies"] or result["sensitive_fields"]):
        raise BridgeError("revocations contain identity only, not provenance, dependencies or content")
    if exported and value.get("verification") != "external_unverified":
        raise BridgeError("external content cannot claim verified status")
    # Selection identifiers are local UI handles, never part of origin/content
    # identity. A relay keeps the source identity while assigning its own handle.
    result["item_sha256"] = content_sha256({key: val for key, val in result.items() if key != "id"})
    if exported and value.get("item_sha256") != result["item_sha256"]:
        raise BridgeError("item integrity mismatch")
    return result


def _load_package(value: Any) -> dict[str, Any]:
    if isinstance(value, (str, bytes)):
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if len(raw) > MAX_BYTES:
            raise BridgeError("package exceeds 2 MiB")
        def pairs(entries: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in entries:
                if key in result:
                    raise BridgeError("duplicate JSON keys")
                result[key] = item
            return result
        try:
            value = json.loads(raw, object_pairs_hook=pairs)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise BridgeError("invalid package JSON") from exc
    if len(canonical_json(value).encode("utf-8")) > MAX_BYTES:
        raise BridgeError("package exceeds 2 MiB")
    if not isinstance(value, dict) or set(value) != _PACKAGE_FIELDS or value.get("schema_version") != SCHEMA:
        raise BridgeError("unsupported package schema or fields")
    unsigned = {key: val for key, val in value.items() if key != "sha256"}
    if value["sha256"] != content_sha256(unsigned):
        raise BridgeError("package integrity mismatch")
    _text(value["package_id"], "package_id", 128)
    _timestamp(value["created_at"], "created_at")
    _namespace(value["source_namespace"])
    if value["mode"] not in {"knowledge_only", "selected_sources"}:
        raise BridgeError("unsupported export mode")
    items = value["items"]
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        raise BridgeError("package requires 1..100 items")
    seen: set[str] = set()
    origins: set[str] = set()
    for item in items:
        _item(item, exported=True)
        if value["mode"] == "knowledge_only" and item["kind"] != "knowledge":
            raise BridgeError("knowledge_only cannot include raw sources")
        identity = _identity(item["source"])
        if item["id"] in seen or identity in origins:
            raise BridgeError("duplicate item or source identity")
        seen.add(item["id"])
        origins.add(identity)
    return json.loads(canonical_json(value))


class BridgeStore:
    """Namespace-isolated two-step approvals and tombstones in a private sidecar."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time, preview_ttl: int = 300):
        if not 1 <= preview_ttl <= 900:
            raise BridgeError("preview_ttl must be 1..900 seconds")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.preview_ttl = preview_ttl
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bridge_revisions(namespace TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS bridge_previews(token_hash TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                  expires REAL NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bridge_items(namespace TEXT NOT NULL, identity TEXT NOT NULL,
                  item_hash TEXT NOT NULL, local_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                  receipt TEXT, PRIMARY KEY(namespace,identity), UNIQUE(namespace,local_id));
                CREATE TABLE IF NOT EXISTS bridge_tombstones(namespace TEXT NOT NULL, identity TEXT NOT NULL,
                  revoked_at REAL NOT NULL, PRIMARY KEY(namespace,identity));
                CREATE TABLE IF NOT EXISTS bridge_plans(plan_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                  created REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bridge_commits(token_hash TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                  plan_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bridge_retry_operations(namespace TEXT NOT NULL, operation_id TEXT NOT NULL,
                  plan_id TEXT NOT NULL, item_id TEXT NOT NULL, local_id TEXT NOT NULL, status TEXT NOT NULL,
                  created REAL NOT NULL, completed REAL, original_receipt TEXT NOT NULL, result_receipt TEXT,
                  error_code TEXT, PRIMARY KEY(namespace,operation_id));
            """)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bridge_previews'").fetchone():
                db.execute("DELETE FROM bridge_previews WHERE expires<=?", (self.clock(),))
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _revision(db: sqlite3.Connection, namespace: str) -> int:
        row = db.execute("SELECT revision FROM bridge_revisions WHERE namespace=?", (namespace,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _bump(db: sqlite3.Connection, namespace: str) -> None:
        db.execute("INSERT INTO bridge_revisions VALUES (?,1) ON CONFLICT(namespace) DO UPDATE SET revision=revision+1", (namespace,))

    def revision(self, namespace: str) -> int:
        with self._connection() as db:
            return self._revision(db, _namespace(namespace))

    def export(self, namespace: str, selected_ids: list[str], mode: str,
               source_resolver: Callable[[str, str], Mapping[str, Any] | None],
               allow_sensitive_fields: list[str] | None = None) -> dict[str, Any]:
        namespace = _namespace(namespace)
        selected = _ids(selected_ids)
        if mode not in {"knowledge_only", "selected_sources"}:
            raise BridgeError("unsupported export mode")
        allowed = set(_ids(allow_sensitive_fields or [], "allow_sensitive_fields", empty=True, limit=3))
        if allowed - SENSITIVE_FIELDS:
            raise BridgeError("unsupported sensitive field")
        items = []
        for item_id in selected:
            raw = source_resolver(namespace, item_id)
            if raw is None:
                raise BridgeError("selected source is missing or inaccessible")
            item = _item(dict(raw))
            if item["id"] != item_id:
                raise BridgeError("resolver returned a different selection identifier")
            if set(item["sensitive_fields"]) - allowed:
                raise BridgeError("selected item contains unapproved sensitive fields")
            items.append(item)
        result = {"schema_version": SCHEMA, "package_id": "pkg_" + secrets.token_hex(16),
                  "created_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                  "source_namespace": namespace, "mode": mode, "items": items}
        result["sha256"] = content_sha256(result)
        return _load_package(result)

    def export_revocations(self, namespace: str, selected_ids: list[str],
                           source_resolver: Callable[[str, str], Mapping[str, Any] | None]) -> dict[str, Any]:
        """Explicitly selected revocations only; never scan or export deleted text."""
        def resolve(scope: str, item_id: str) -> Mapping[str, Any] | None:
            original = source_resolver(scope, item_id)
            if original is None:
                return None
            return {"id": item_id, "kind": "revocation", "text": "", "source": original["source"],
                    "role": original.get("role", "knowledge"), "occurred_at": original.get("occurred_at", "")}
        return self.export(namespace, selected_ids, "selected_sources", resolve)

    def preview(self, namespace: str, package: Any, selected_ids: list[str],
                allow_sensitive_fields: list[str] | None = None,
                external_target_revision: str | int | None = None,
                authorize_records: Callable[[str, list[dict[str, Any]]], None] | None = None) -> dict[str, Any]:
        namespace = _namespace(namespace)
        package = _load_package(package)
        selected = sorted(_ids(selected_ids))
        by_id = {item["id"]: item for item in package["items"]}
        if set(selected) - set(by_id):
            raise BridgeError("selection contains an unknown package item")
        allowed = set(_ids(allow_sensitive_fields or [], "allow_sensitive_fields", empty=True, limit=3))
        if allowed - SENSITIVE_FIELDS:
            raise BridgeError("unsupported sensitive field")
        now = self.clock()
        if external_target_revision is not None and (isinstance(external_target_revision, bool) or not isinstance(external_target_revision, (str, int))):
            raise BridgeError("external_target_revision must be a string or integer")
        if isinstance(external_target_revision, str):
            _text(external_target_revision, "external_target_revision", 256)
        records, conflicts, blocked, revocations = [], [], [], []
        with self._connection() as db:
            db.execute("DELETE FROM bridge_previews WHERE expires<=?", (now,))
            revision = self._revision(db, namespace)
            for item_id in selected:
                item = by_id[item_id]
                identity = _identity(item["source"])
                revoke = item["kind"] == "revocation"
                local_id = "bridge_" + content_sha256([namespace, "revoke" if revoke else "import", identity])[:40]
                lookup_identity = "revoke:" + identity if revoke else identity
                existing = db.execute("SELECT * FROM bridge_items WHERE namespace=? AND identity=?", (namespace, lookup_identity)).fetchone()
                reason = []
                if set(item["sensitive_fields"]) - allowed:
                    reason.append("sensitive_fields_not_approved")
                for origin in ([] if revoke else [item["source"], *item["provenance"]]):
                    if db.execute("SELECT 1 FROM bridge_tombstones WHERE namespace=? AND identity=?", (namespace, _identity(origin))).fetchone():
                        reason.append("source_revoked")
                        break
                missing = [dep for dep in item["dependencies"] if dep not in selected]
                if missing:
                    reason.append("missing_selected_dependencies")
                if item_id in item["dependencies"]:
                    reason.append("self_dependency")
                if existing and existing["item_hash"] != item["item_sha256"]:
                    conflicts.append({"item_id": item_id, "source": item["source"],
                                      "existing_hash": existing["item_hash"], "incoming_hash": item["item_sha256"]})
                if reason:
                    blocked.append({"item_id": item_id, "reasons": reason, "missing_dependencies": missing})
                records.append({"item_id": item_id, "local_id": local_id, "record": item,
                                "disposition": "duplicate" if existing and existing["item_hash"] == item["item_sha256"] else "new",
                                "status": existing["status"] if existing and existing["item_hash"] == item["item_sha256"] else "pending"})
                if revoke:
                    affected = self._affected(db, namespace, {identity})
                    targets = []
                    for row in db.execute("SELECT identity,local_id,receipt FROM bridge_items WHERE namespace=?", (namespace,)):
                        if row["identity"] in affected:
                            receipt = json.loads(row["receipt"]) if row["receipt"] else {}
                            targets.append({"local_id": row["local_id"], "local_message_id": receipt.get("local_message_id"),
                                            "source_identity_sha256": row["identity"]})
                    revocations.append({"item_id": item_id, "source": item["source"], "targets": targets,
                                        "warning": "Explicit approval revokes this external source and dependent imported records."})
            # No invented dependency nodes and no circular proof chains.
            visiting, visited = set(), set()
            def visit(item_id: str) -> None:
                if item_id in visiting:
                    raise BridgeError("cyclic package dependencies")
                if item_id in visited:
                    return
                visiting.add(item_id)
                for dependency in by_id[item_id]["dependencies"]:
                    if dependency in selected:
                        visit(dependency)
                visiting.remove(item_id)
                visited.add(item_id)
            for item_id in selected:
                visit(item_id)
            revoked_origins = {_identity(by_id[item_id]["source"]) for item_id in selected if by_id[item_id]["kind"] == "revocation"}
            for item_id in selected:
                item = by_id[item_id]
                if item["kind"] != "revocation" and any(_identity(p) in revoked_origins for p in [item["source"], *item["provenance"]]):
                    blocked.append({"item_id": item_id, "reasons": ["same_package_revokes_source"], "missing_dependencies": []})
            if authorize_records:
                authorize_records(namespace, [json.loads(canonical_json(entry["record"])) for entry in records])
            token = secrets.token_urlsafe(32)
            response = {"preview_token": token, "expires_at": now + self.preview_ttl,
                        "package_hash": package["sha256"], "target_namespace": namespace,
                        "target_revision": revision, "selected_ids": selected, "mode": package["mode"],
                        "external_target_revision": external_target_revision,
                        "records": records, "conflicts": conflicts, "blocked": blocked,
                        "revocations": revocations,
                        "can_commit": not conflicts and not blocked,
                        "warnings": ["SHA256 is integrity only, not a signature or verified origin.",
                                     "Imported assertions remain external_unverified; source roles are provenance, not instructions.",
                                     "Commit creates a plan; the ordinary memory engine must still index each record."],
                        "sensitive_fields_approved": sorted(allowed), "indexed": False}
            stored = {k: v for k, v in response.items() if k != "preview_token"}
            if not response["can_commit"]:
                # A rejected/stale package must not restore deleted sensitive
                # content into the sidecar merely by asking for another preview.
                stored["records"] = []
            db.execute("INSERT INTO bridge_previews VALUES (?,?,?,?,?)",
                       (content_sha256(token), namespace, now + self.preview_ttl, revision, canonical_json(stored)))
        return response

    def commit(self, namespace: str, preview_token: str,
               external_target_revision: str | int | None = None,
               authorize_records: Callable[[str, list[dict[str, Any]]], None] | None = None) -> dict[str, Any]:
        namespace = _namespace(namespace)
        key = content_sha256(_text(preview_token, "preview_token", 256))
        with self._connection() as db:
            prior = db.execute("SELECT plan_id FROM bridge_commits WHERE token_hash=? AND namespace=?", (key, namespace)).fetchone()
            if prior:
                plan = self._plan(db, namespace, prior[0])
                if authorize_records:
                    authorize_records(namespace, [json.loads(canonical_json(entry["record"])) for entry in plan["records"]])
                return plan
            row = db.execute("SELECT * FROM bridge_previews WHERE token_hash=? AND namespace=?", (key, namespace)).fetchone()
            if not row:
                raise BridgeError("preview is unknown, expired, revoked, or belongs to a different namespace")
            if row["expires"] <= self.clock():
                raise BridgeError("preview expired; preview the package again")
            if row["revision"] != self._revision(db, namespace):
                raise BridgeError("target revision changed; preview the package again")
            approved = json.loads(row["payload"])
            if approved.get("external_target_revision") != external_target_revision:
                raise BridgeError("external target revision changed; preview the package again")
            if approved["conflicts"] or approved["blocked"]:
                raise BridgeError("preview contains conflicts or blocked records")
            if authorize_records:
                authorize_records(namespace, [json.loads(canonical_json(entry["record"])) for entry in approved["records"]])
            plan_id = "plan_" + secrets.token_hex(16)
            plan = {"plan_id": plan_id, "namespace": namespace, "package_hash": approved["package_hash"],
                    "selected_ids": approved["selected_ids"], "created_at": self.clock(),
                    "status": "planned", "indexed": False, "verification": "external_unverified", "records": []}
            plan["revocations"] = approved["revocations"]
            # Revocation is an irreversible local import fence. Actual engine deletion
            # remains a pending, separately acknowledged plan action.
            for revocation in approved["revocations"]:
                self._revoke(db, namespace, {_identity(revocation["source"])})
            for entry in approved["records"]:
                item = entry["record"]
                identity = _identity(item["source"])
                if item["kind"] == "revocation":
                    identity = "revoke:" + identity
                db.execute("INSERT OR IGNORE INTO bridge_items VALUES (?,?,?,?,?,?,NULL)",
                           (namespace, identity, item["item_sha256"], entry["local_id"], canonical_json(item), "pending"))
                plan["records"].append({"item_id": entry["item_id"], "local_id": entry["local_id"],
                                        "record": item, "resolved_dependencies": {
                                            dep: next(r["local_id"] for r in approved["records"] if r["item_id"] == dep)
                                            for dep in item["dependencies"]}})
            db.execute("INSERT INTO bridge_plans VALUES (?,?,?,?)", (plan_id, namespace, self.clock(), canonical_json(plan)))
            db.execute("INSERT INTO bridge_commits VALUES (?,?,?)", (key, namespace, plan_id))
            db.execute("DELETE FROM bridge_previews WHERE token_hash=?", (key,))
            self._bump(db, namespace)
            return self._plan(db, namespace, plan_id)

    @staticmethod
    def _plan(db: sqlite3.Connection, namespace: str, plan_id: str) -> dict[str, Any]:
        row = db.execute("SELECT payload FROM bridge_plans WHERE namespace=? AND plan_id=?", (namespace, plan_id)).fetchone()
        if not row:
            raise BridgeError("import plan not found in this namespace")
        plan = json.loads(row[0])
        if plan["status"] == "revoked":
            return plan
        for entry in plan["records"]:
            state = db.execute("SELECT status,receipt FROM bridge_items WHERE namespace=? AND local_id=?", (namespace, entry["local_id"])).fetchone()
            if not state:
                raise BridgeError("import record was revoked")
            entry["status"] = state["status"]
            entry["receipt"] = json.loads(state["receipt"]) if state["receipt"] else None
            entry["retry_history"] = [{"operation_id": attempt["operation_id"], "status": attempt["status"],
                "created_at": attempt["created"], "completed_at": attempt["completed"],
                "original_receipt": json.loads(attempt["original_receipt"]),
                "result_receipt": json.loads(attempt["result_receipt"]) if attempt["result_receipt"] else None,
                "error_code": attempt["error_code"]}
                for attempt in db.execute("SELECT * FROM bridge_retry_operations WHERE namespace=? AND local_id=? ORDER BY created,operation_id", (namespace, entry["local_id"]))]
        completed = sum(r["status"] == "applied" for r in plan["records"])
        accepted = sum(r["status"] in {"applied", "accepted_pending"} for r in plan["records"])
        plan["indexed"] = bool(plan["records"]) and completed == len(plan["records"]) and not plan.get("revocations")
        plan["status"] = "applied" if completed == len(plan["records"]) else "accepted_pending" if accepted == len(plan["records"]) else "partial" if accepted else "planned"
        return plan

    def get_plan(self, namespace: str, plan_id: str) -> dict[str, Any]:
        with self._connection() as db:
            return self._plan(db, _namespace(namespace), _text(plan_id, "plan_id"))

    def get_imported_by_local_id(self, namespace: str, local_message_id: str) -> dict[str, Any] | None:
        """Recover original origin for an explicitly selected relay export."""
        namespace = _namespace(namespace)
        local_message_id = _text(local_message_id, "local_message_id")
        with self._connection() as db:
            for row in db.execute("SELECT local_id,payload,receipt FROM bridge_items WHERE namespace=?", (namespace,)):
                receipt = json.loads(row["receipt"]) if row["receipt"] else {}
                if row["local_id"] == local_message_id or receipt.get("local_message_id") == local_message_id:
                    record = json.loads(row["payload"])
                    return record if record["kind"] != "revocation" else None
        return None

    def list_plans(self, namespace: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise BridgeError("limit must be 1..100")
        with self._connection() as db:
            namespace = _namespace(namespace)
            rows = db.execute("SELECT plan_id FROM bridge_plans WHERE namespace=? ORDER BY created DESC,plan_id LIMIT ?", (namespace, limit)).fetchall()
            return [self._plan(db, namespace, row[0]) for row in rows]

    def mark_applied(self, namespace: str, plan_id: str, item_id: str, engine_receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Trusted adapter only: invoke AFTER normal ingest succeeds, never from package JSON."""
        namespace = _namespace(namespace)
        if not isinstance(engine_receipt, Mapping) or set(engine_receipt) - {"local_message_id", "indexed", "receipt_id"}:
            raise BridgeError("engine receipt requires local_message_id and boolean indexed")
        if not isinstance(engine_receipt.get("indexed"), bool):
            raise BridgeError("engine indexing status must be explicit")
        receipt = {"local_message_id": _text(engine_receipt.get("local_message_id"), "local_message_id"), "indexed": engine_receipt["indexed"]}
        if "receipt_id" in engine_receipt:
            receipt["receipt_id"] = _text(engine_receipt["receipt_id"], "receipt_id")
        if not receipt["indexed"] and not receipt.get("receipt_id"):
            raise BridgeError("pending engine acceptance requires its job receipt_id")
        with self._connection() as db:
            plan = self._plan(db, namespace, _text(plan_id, "plan_id"))
            if plan["status"] == "revoked":
                raise BridgeError("import plan revoked")
            entry = next((r for r in plan["records"] if r["item_id"] == item_id), None)
            if not entry:
                raise BridgeError("item does not belong to this plan")
            if entry["record"]["kind"] == "revocation":
                raise BridgeError("revocation needs mark_revocation_applied, not indexing")
            if entry["status"] == "applied":
                if entry["receipt"] != receipt:
                    raise BridgeError("applied receipt cannot be replaced")
                return plan
            if entry["status"] == "accepted_pending" and (entry["receipt"]["local_message_id"] != receipt["local_message_id"] or entry["receipt"].get("receipt_id") != receipt.get("receipt_id")):
                raise BridgeError("accepted job receipt cannot be replaced")
            for local_id in entry["resolved_dependencies"].values():
                dependency = db.execute("SELECT status FROM bridge_items WHERE namespace=? AND local_id=?", (namespace, local_id)).fetchone()
                if not dependency or dependency[0] != "applied":
                    raise BridgeError("dependency has not been indexed")
            db.execute("UPDATE bridge_items SET status=?,receipt=? WHERE namespace=? AND local_id=?",
                       ("applied" if receipt["indexed"] else "accepted_pending", canonical_json(receipt), namespace, entry["local_id"]))
            self._bump(db, namespace)
            return self._plan(db, namespace, plan_id)

    def mark_revocation_applied(self, namespace: str, plan_id: str, item_id: str,
                                deleted_message_ids: list[str]) -> dict[str, Any]:
        """Trusted adapter acknowledges only actual deletion/absence of every target."""
        namespace = _namespace(namespace)
        deleted = sorted(_ids(deleted_message_ids, "deleted_message_ids", empty=True, limit=10000))
        with self._connection() as db:
            plan = self._plan(db, namespace, _text(plan_id, "plan_id"))
            if plan["status"] == "revoked":
                raise BridgeError("import plan revoked")
            entry = next((r for r in plan["records"] if r["item_id"] == item_id and r["record"]["kind"] == "revocation"), None)
            revocation = next((r for r in plan.get("revocations", []) if r["item_id"] == item_id), None)
            if not entry or not revocation:
                raise BridgeError("revocation does not belong to this plan")
            expected = sorted(set(t["local_message_id"] for t in revocation["targets"] if t["local_message_id"]))
            if deleted != expected:
                raise BridgeError("not all approved revocation targets were deleted")
            receipt = {"deleted_message_ids": deleted, "deleted": True, "indexed": False}
            db.execute("UPDATE bridge_items SET status='applied',receipt=? WHERE namespace=? AND local_id=?",
                       (canonical_json(receipt), namespace, entry["local_id"]))
            self._bump(db, namespace)
            return self._plan(db, namespace, plan_id)

    def reserve_retry(self, namespace: str, plan_id: str, item_id: str, operation_id: str,
                      expected_receipt: Mapping[str, Any] | None, engine_state: str) -> dict[str, Any]:
        """Trusted service only: reserve one explicit attempt before external work.

        A reserved operation with unknown outcome must not be executed twice.
        The backend owns the existing job and its recovery audit; this store
        neither creates a replacement source nor marks indexing successful.
        """
        namespace = _namespace(namespace)
        operation_id = _text(operation_id, "operation_id", 128)
        plan_id, item_id = _text(plan_id, "plan_id"), _text(item_id, "item_id")
        with self._connection() as db:
            prior = db.execute("SELECT * FROM bridge_retry_operations WHERE namespace=? AND operation_id=?", (namespace, operation_id)).fetchone()
            if prior:
                if prior["plan_id"] != plan_id or prior["item_id"] != item_id:
                    raise BridgeError("retry operation_id already belongs to a different record")
                return {"operation_id": operation_id, "status": prior["status"], "existing": True,
                        "error_code": prior["error_code"], "receipt": json.loads(prior["result_receipt"]) if prior["result_receipt"] else None}
            plan = self._plan(db, namespace, plan_id)
            entry = next((r for r in plan["records"] if r["item_id"] == item_id), None)
            if plan["status"] == "revoked" or not entry or entry["record"]["kind"] == "revocation":
                raise BridgeError("retry requires a live imported record")
            if entry["status"] != "accepted_pending" or not entry["receipt"] or entry["receipt"] != expected_receipt:
                raise BridgeError("retry requires the unchanged accepted job receipt")
            if engine_state != "failed":
                raise BridgeError("only a confirmed failed ingest job can be explicitly retried")
            attempts = db.execute("SELECT status FROM bridge_retry_operations WHERE namespace=? AND local_id=?", (namespace, entry["local_id"])).fetchall()
            if any(row[0] == "reserved" for row in attempts):
                raise BridgeError("prior retry outcome is unknown; inspect the existing backend job")
            if len(attempts) >= MAX_EXPLICIT_RETRIES:
                raise BridgeError("explicit retry limit reached; inspect backend recovery evidence")
            db.execute("INSERT INTO bridge_retry_operations VALUES (?,?,?,?,?,'reserved',?,NULL,?,NULL,NULL)",
                       (namespace, operation_id, plan_id, item_id, entry["local_id"], self.clock(), canonical_json(dict(expected_receipt))))
            self._bump(db, namespace)
            return {"operation_id": operation_id, "status": "reserved", "existing": False,
                    "receipt": dict(expected_receipt)}

    def finish_retry(self, namespace: str, operation_id: str, receipt: Mapping[str, Any]) -> None:
        namespace = _namespace(namespace)
        operation_id = _text(operation_id, "operation_id", 128)
        with self._connection() as db:
            row = db.execute("SELECT * FROM bridge_retry_operations WHERE namespace=? AND operation_id=?", (namespace, operation_id)).fetchone()
            if not row or row["status"] != "reserved":
                raise BridgeError("retry reservation is not active")
            original = json.loads(row["original_receipt"])
            if not isinstance(receipt, Mapping) or set(receipt) != set(original) or dict(receipt) != original or receipt.get("indexed") is not False:
                raise BridgeError("retry must preserve the original source and job receipt; indexing is verified separately")
            item = db.execute("SELECT status FROM bridge_items WHERE namespace=? AND local_id=?", (namespace, row["local_id"])).fetchone()
            if not item or item["status"] != "accepted_pending":
                raise BridgeError("retry record changed or was revoked")
            db.execute("UPDATE bridge_retry_operations SET status='accepted',completed=?,result_receipt=? WHERE namespace=? AND operation_id=?",
                       (self.clock(), canonical_json(dict(receipt)), namespace, operation_id))
            self._bump(db, namespace)

    def reject_retry(self, namespace: str, operation_id: str) -> None:
        """Keep a content-free rejection receipt; never persist provider error text."""
        namespace = _namespace(namespace)
        operation_id = _text(operation_id, "operation_id", 128)
        with self._connection() as db:
            db.execute("UPDATE bridge_retry_operations SET status='rejected',completed=?,error_code='backend_retry_rejected' WHERE namespace=? AND operation_id=? AND status='reserved'",
                       (self.clock(), namespace, operation_id))
            self._bump(db, namespace)

    def revoke(self, namespace: str, source_system: str, source_namespace: str, source_id: str) -> dict[str, Any]:
        namespace = _namespace(namespace)
        origin = _source({"system": source_system, "namespace": source_namespace, "id": source_id})
        with self._connection() as db:
            result = self._revoke(db, namespace, {_identity(origin)})
        self._compact()
        return result

    @staticmethod
    def _affected(db: sqlite3.Connection, namespace: str, identities: set[str]) -> set[str]:
        identities = set(identities)
        rows = db.execute("SELECT * FROM bridge_items WHERE namespace=?", (namespace,)).fetchall()
        plans = [json.loads(row[0]) for row in db.execute("SELECT payload FROM bridge_plans WHERE namespace=?", (namespace,))]
        changed = True
        while changed:
            changed = False
            for row in rows:
                item = json.loads(row["payload"])
                if row["identity"] not in identities and any(_identity(p) in identities for p in item["provenance"]):
                    identities.add(row["identity"])
                    changed = True
            local_ids = {row["local_id"] for row in rows if row["identity"] in identities}
            by_local = {row["local_id"]: row["identity"] for row in rows}
            for plan in plans:
                for entry in plan["records"]:
                    identity = by_local.get(entry["local_id"])
                    if identity and identity not in identities and any(local_id in local_ids for local_id in entry.get("resolved_dependencies", {}).values()):
                        identities.add(identity)
                        changed = True
        return identities

    def _revoke(self, db: sqlite3.Connection, namespace: str, identities: set[str]) -> dict[str, Any]:
        identities = self._affected(db, namespace, identities)
        rows = db.execute("SELECT * FROM bridge_items WHERE namespace=?", (namespace,)).fetchall()
        local_ids = {row["local_id"] for row in rows if row["identity"] in identities}
        for identity in identities:
            db.execute("INSERT OR IGNORE INTO bridge_tombstones VALUES (?,?,?)", (namespace, identity, self.clock()))
            db.execute("DELETE FROM bridge_items WHERE namespace=? AND identity=?", (namespace, identity))
        # Revoke and scrub complete approval snapshots, not only the selected row.
        db.execute("DELETE FROM bridge_previews WHERE namespace=?", (namespace,))
        for row in db.execute("SELECT plan_id,payload FROM bridge_plans WHERE namespace=?", (namespace,)).fetchall():
            plan = json.loads(row["payload"])
            if any(entry["local_id"] in local_ids for entry in plan["records"]):
                plan.update(status="revoked", indexed=False, records=[], selected_ids=[], revocations=[])
                db.execute("UPDATE bridge_plans SET payload=? WHERE plan_id=?", (canonical_json(plan), row["plan_id"]))
        self._bump(db, namespace)
        return {"namespace": namespace, "revoked_identities": len(identities), "purged_records": len(local_ids), "indexed": False}

    def revoke_imported_by_local_id(self, namespace: str, local_message_id: str) -> dict[str, Any]:
        namespace = _namespace(namespace)
        local_message_id = _text(local_message_id, "local_message_id")
        with self._connection() as db:
            identities = set()
            for row in db.execute("SELECT identity,local_id,receipt FROM bridge_items WHERE namespace=?", (namespace,)):
                receipt = json.loads(row["receipt"]) if row["receipt"] else {}
                if row["local_id"] == local_message_id or receipt.get("local_message_id") == local_message_id:
                    identities.add(row["identity"])
            result = self._revoke(db, namespace, identities) if identities else {"namespace": namespace, "revoked_identities": 0, "purged_records": 0, "indexed": False}
        self._compact()
        return result

    def purge_namespace(self, namespace: str) -> dict[str, Any]:
        namespace = _namespace(namespace)
        with self._connection() as db:
            identities = {row[0] for row in db.execute("SELECT identity FROM bridge_items WHERE namespace=?", (namespace,))}
            result = self._revoke(db, namespace, identities)
        self._compact()
        return result

    def _compact(self) -> None:
        # The sidecar is DELETE-journal SQLite; VACUUM also removes old payload pages.
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute("VACUUM")
