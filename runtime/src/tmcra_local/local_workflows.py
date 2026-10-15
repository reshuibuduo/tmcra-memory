"""Owner-local binding; packages always enter ordinary project-scoped ingestion."""
import hashlib
import json
import sqlite3
from .workflow_service import WorkflowService
from .memory_bridge import BridgeError


def local_workflows(engine):
    def resolve(namespace, identifier):
        row = engine.store.message(identifier)
        if not row or row.get("deleted_at") or row["project_id"] != namespace:
            return None
        return {"id": identifier, "message_id": identifier, "text": row["content"],
                "role": row["role"], "occurred_at": row["occurred_at"], "title": row.get("session_title", "")}

    def sources(namespace):
        return [{"id": r["message_id"], "kind": "source", "title": r["content"][:100],
                 "role": r["role"], "occurred_at": r["occurred_at"]}
                for r in engine.store.messages(project_id=namespace, limit=50)]

    def ingest(namespace, entry):
        record = entry["record"]
        # Imported source roles remain metadata. No imported system/tool text
        # gets promoted into a new local privileged actor or verified fact.
        text = "[Imported memory: external_unverified; original role=" + record["role"] + "]\n" + record["text"]
        result = engine.ingest_message(project_id=namespace, session_id="memory-package-" + hashlib.sha256(namespace.encode()).hexdigest()[:32], role="user", content=text,
            source_app="memory-package", native_thread_id=record["source"]["namespace"], native_message_id=entry["local_id"],
            message_id=entry["local_id"], occurred_at=record["occurred_at"], visibility="project")
        scopes = engine.store.scopes_for_message(entry["local_id"])
        indexed = any(s["scope_id"] == engine.project_scope(namespace) and s["status"] == "committed" for s in scopes)
        if not indexed:
            raise ValueError("Local writer has not committed the imported source")
        return {"local_message_id": entry["local_id"], "indexed": True}

    def delete(namespace, entry):
        deleted = []
        for identifier in entry.get("revoked_local_ids", []):
            row = engine.store.message(identifier)
            if row and row["project_id"] != namespace:
                raise BridgeError("Revocation target belongs to a different project")
            if row and not row.get("deleted_at"):
                engine.delete_message(identifier)
            remaining = engine.store.message(identifier)
            if remaining and not remaining.get("deleted_at"):
                raise BridgeError("Memory engine has not deleted the approved target")
            deleted.append(identifier)
        return {"deleted_message_ids": deleted, "pending": []}

    def revision(namespace):
        digest = hashlib.sha256()
        with sqlite3.connect(engine.store.path) as db:
            for row in db.execute("SELECT message_id,content_sha256,occurred_at,deleted_at FROM messages WHERE project_id=? ORDER BY message_id", (namespace,)):
                digest.update(json.dumps(row, ensure_ascii=False).encode())
        return digest.hexdigest()

    return WorkflowService(engine.state_dir / "workflows", resolve, sources, ingest, delete,
                           revision_resolver=revision, operation_lock=engine._workflow_lock)
