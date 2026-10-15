from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .actor_provenance import normalize_message_actor_metadata, origin_metadata, source_authority, permits_human_preference
from .auth import restrict_owner_directory
from .core import RealGraphFactory, V4BatchStore, V4BatchWriter
from .core_loader import graph_core_root
from .evidence_view import EvidenceViewError, build_prompt_evidence
from .llm import OpenAICompatibleClient
from .personal_knowledge import (
    PERSONAL_KNOWLEDGE_SYSTEM_PROMPT,
    build_personal_knowledge_batches,
    build_personal_knowledge_fallback,
    merge_personal_knowledge_batches,
    personal_knowledge_source_fingerprint,
    sanitize_personal_knowledge_grounding,
    validate_personal_knowledge_batch,
)
from .runtime_env import build_service_environment, load_local_runtime_config
from .storage import LocalStore, utc_now
from .ingest_queue import DurableIngestQueue, InferenceLock, ProcessWriterLease, SourceChanged, classify_failure
from .runtime_modes import runtime_mode, read_only_environment, RuntimeModeError
from .visual_atlas import build_visual_atlas, validate_visual_atlas


class LocalEngineError(RuntimeError):
    pass


_PROCESS_SECRET_ENV_KEYS = {
    "TMCRA_WRITER_API_KEY_POOL",
    "TMCRA_WRITER_REVIEWER_API_KEY_POOL",
}


def _clean(value: Any, *, maximum: int = 0) -> str:
    result = str(value or "").strip()
    if maximum and len(result) > maximum:
        raise LocalEngineError(f"value exceeds {maximum} characters")
    if any(character in result for character in "\0\r\n"):
        raise LocalEngineError("identifier contains a control character")
    return result


def _record_payload(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        payload = dict(record.to_dict())
    else:
        payload = dict(vars(record))
    metadata = payload.get("metadata")
    payload["metadata"] = dict(metadata or {}) if isinstance(metadata, Mapping) else {}
    return payload


def _lexical_terms(value: Any) -> set[str]:
    text = str(value or "").casefold()
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text))


class LocalMemoryEngine:
    """Complete owner-local TMCRA write, recall, graph, and knowledge runtime."""

    def __init__(self, config_path: str | Path, *, verify_models: bool = True, runtime_mode_override=None) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = load_local_runtime_config(self.config_path)
        self.runtime_mode = runtime_mode(self.config, runtime_mode_override)
        if self.runtime_mode == 'off':
            raise RuntimeModeError('off', 'service startup')
        self.environment = (read_only_environment(self.config, self.config_path)
            if self.runtime_mode == 'read-only' else build_service_environment(
                self.config, config_path=self.config_path, require_model=verify_models))
        # The imported graph core reads its non-secret runtime switches from the
        # process environment. Provider credentials stay on the in-process LLM
        # client so subprocesses started later cannot inherit the user's key.
        os.environ.update(
            {
                name: value
                for name, value in self.environment.items()
                if name not in _PROCESS_SECRET_ENV_KEYS
            }
        )
        os.environ["TMCRA_LOCAL_INCREMENTAL_BATCH_IDS"] = "1"
        state_dir = Path(self.environment["TMCRA_SERVICE_STATE_DIR"]).resolve()
        restrict_owner_directory(state_dir)
        self.state_dir = state_dir
        self.store = LocalStore(state_dir / "local.sqlite3")
        # The V4 writer journal and graph tables intentionally share one
        # SQLite file. Source persistence and journal finalization are one
        # atomic transaction inside the graph adapter.
        algorithm_database = state_dir / "memory.sqlite3"
        self.writer_store = V4BatchStore(algorithm_database)
        self._lock = InferenceLock()
        self._workflow_lock = threading.RLock()
        self._writer_lease = ProcessWriterLease(state_dir / '.writer.lock')
        self.jobs = DurableIngestQueue(self.store)
        self._worker = None
        self._worker_stop = threading.Event()
        self._worker_wake = threading.Event()
        self._worker_guard = threading.Lock()
        self._reader_factory = None
        self._reader_settings = None
        self._usage_context = threading.local()
        self.graph_factory = RealGraphFactory(
            repo=graph_core_root(),
            database=algorithm_database,
            retrieval_mode=self.environment.get(
                "TMCRA_GRAPH_RETRIEVAL_MODE", "hybrid_node_scored"
            ),
            node_model_path=self.environment.get("TMCRA_GRAPH_NODE_MODEL_PATH", ""),
            path_model_path=self.environment.get("TMCRA_GRAPH_PATH_MODEL_PATH", ""),
            node_model_device=self.environment.get("TMCRA_SERVICE_GRAPH_DEVICE", ""),
            graph_environment={
                key: value
                for key, value in self.environment.items()
                if key.startswith("TMCRA_EMBEDDER_")
                or key.startswith("TMCRA_WRITE_EMBEDDER_")
            },
        )
        if self.runtime_mode == 'read-only':
            self.llm = None
            self.writer = None
            return
        llm = OpenAICompatibleClient(
            base_url=self.environment["TMCRA_WRITER_BASE_URL"],
            model=self.environment["TMCRA_WRITER_MODEL"],
            api_key=self.environment.get("TMCRA_WRITER_API_KEY_POOL", ""),
            timeout=float(self.environment.get("TMCRA_WRITER_TIMEOUT_SECONDS", "180")),
            max_tokens=int(self.environment.get("TMCRA_WRITER_MAX_TOKENS", "8192")),
            response_format="json_object",
            usage_sink=self._record_usage,
            provider=self.environment.get("TMCRA_WRITER_PROVIDER", "openai-compatible"),
        )
        self.llm = llm
        self.writer = V4BatchWriter(
            store=self.writer_store,
            flash_client=llm,
            pro_client=llm,
            graph_factory=self.graph_factory,
            log_dir=None,
        )

    def require_enrich(self, operation="write"):
        if self.runtime_mode != 'enrich':
            raise RuntimeModeError(self.runtime_mode, operation)

    def _read_backend(self, scope_id):
        # Never return the writer's possibly uncommitted mutation_batch objects.
        factory = self.graph_factory
        settings = (factory.retrieval_mode, factory.node_model_path, factory.path_model_path,
                    factory.node_model_device, tuple(sorted(factory.graph_environment.items())))
        if self._reader_factory is None or self._reader_settings != settings:
            self._reader_factory = RealGraphFactory(repo=factory.repo, database=factory.database,
                retrieval_mode=factory.retrieval_mode, node_model_path=factory.node_model_path,
                path_model_path=factory.path_model_path, node_model_device=factory.node_model_device,
                graph_environment=factory.graph_environment)
            self._reader_settings = settings
        backend = self._reader_factory.for_scope(scope_id)
        backend._reload(force=True)
        return backend

    @contextmanager
    def _model_boundary(self, message_id=None):
        epoch = self.store.privacy_revision()
        with self._lock.suspended():
            yield
        # Reacquired graph lock before interpreting or committing model output.
        row = self.store.message(message_id) if message_id else None
        if message_id and (not row or row.get('deleted_at')):
            raise SourceChanged('source deletion invalidated the in-flight model result', source_deleted=True)
        if self.store.privacy_revision() != epoch:
            raise SourceChanged('context deletion invalidated the in-flight model result')

    @contextmanager
    def _writer_model_boundaries(self, message_id):
        engine = self
        class ClientBoundary:
            def __init__(self, client):
                self.client = client
            def __getattr__(self, name):
                value = getattr(self.client, name)
                if name not in {'complete', 'reconcile', 'complete_json'} or not callable(value):
                    return value
                def invoke(*args, **kwargs):
                    with engine._model_boundary(message_id):
                        return value(*args, **kwargs)
                return invoke
        originals = (self.writer.flash_client, self.writer.pro_client)
        self.writer.flash_client, self.writer.pro_client = map(ClientBoundary, originals)
        try:
            yield
        finally:
            self.writer.flash_client, self.writer.pro_client = originals

    def enqueue_message(self, **values):
        self.require_enrich('enqueue')
        values = dict(values)
        scopes = self._scope_ids(values['project_id'], values.pop('visibility', 'project'))
        values['actor'] = normalize_message_actor_metadata(values['role'], values.get('actor') or {})
        for key in ('project_title', 'session_title', 'occurred_at', 'message_id'):
            values.setdefault(key, '')
        values.setdefault('source_app', 'manual')
        values.setdefault('native_thread_id', values['session_id'])
        values.setdefault('native_message_id', '')
        with self._lock:
            message, _ = self.store.register_message(**values, _after_register=self.jobs.admission(scopes))
        result = self.job_status(self.jobs.for_message(message['message_id'])['job_id'])
        self.start_worker()
        self._worker_wake.set()
        return result

    def job_status(self, job_id):
        with self._lock:
            result = self.jobs.get(job_id)
            if result is None:
                return None
            row = self.store.message(result['message_id'])
            derived = False
            processing = 'pending'
            if result['status'] == 'cancelled':
                processing = 'cancelled'
            elif result['indexed'] and row and not row.get('deleted_at'):
                processing = ('skipped_origin_unverified' if row['role'] == 'user'
                    and not permits_human_preference(row.get('actor', {})) else 'completed')
                for scope in self.store.scopes_for_message(row['message_id']):
                    backend = self._read_backend(scope['scope_id'])
                    records = backend.adapter.graph.records_by_id
                    source_ids = {key for key, record in records.items()
                        if _record_payload(record).get('metadata', {}).get('message_id') == row['message_id']
                        and _record_payload(record).get('metadata', {}).get('content_variant') == 'source_message'}
                    for record in records.values():
                        meta = _record_payload(record).get('metadata', {})
                        refs = set(meta.get('source_record_ids') or []) | {meta.get('source_record_id')}
                        if meta.get('content_variant') != 'source_message' and refs & source_ids:
                            derived = True
            elif result['status'] in {'retryable_failed', 'permanent_failed', 'interrupted'}:
                processing = result['status']
            result.update(derived_indexed=derived, semantic_processing=processing)
            return result

    def retry_job(self, job_id, *, confirmed=False):
        self.require_enrich('retry')
        result = self.jobs.retry(job_id, confirmed=confirmed)
        self.start_worker(); self._worker_wake.set()
        return self.job_status(job_id)

    def start_worker(self):
        if self.runtime_mode != 'enrich':
            return
        with self._worker_guard:
            if self._worker and self._worker.is_alive():
                return
            self._worker_stop.clear()
            self._worker = threading.Thread(target=self._worker_loop, name='tmcra-ingest-worker', daemon=True)
            self._worker.start()

    def close(self):
        self._worker_stop.set(); self._worker_wake.set()
        if self._worker:
            self._worker.join(timeout=2)

    def _worker_loop(self):
        while not self._worker_stop.is_set():
            try:
                processed = self.run_next_job()
            except Exception:
                # Running receipt remains durable and recoverable; do not spin.
                processed = False
            if not processed:
                self._worker_wake.wait(.25)
                self._worker_wake.clear()

    def run_next_job(self):
        self.require_enrich('process jobs')
        with self._writer_lease.acquire(blocking=False) as acquired:
            if not acquired:
                return False
            self.jobs.recover_interrupted()
            job = self.jobs.claim()
            if job is None:
                return False
            message = self.store.message(job['message_id'])
            if not message or message.get('deleted_at'):
                self.jobs.finish(job['message_id'], 'cancelled', 'source_deleted')
                return True
            scope_ids = json.loads(job['scope_ids_json'])
            if job['attempts'] > 0:
                # Only an explicitly requeued attempt may discard an incomplete
                # writer journal and issue a new call after an unknown outcome.
                for scope in scope_ids:
                    old = self.store.scope_status(message['message_id'], scope)
                    if old and old['status'] != 'committed':
                        self.store.set_scope_status(message_id=message['message_id'], scope_id=scope,
                                                    status='failed', error='explicit_retry')
            visibility = 'both' if len(scope_ids) == 2 else ('global' if scope_ids == [self.global_scope()] else 'project')
            try:
                self.ingest_message(project_id=message['project_id'], session_id=message['session_id'],
                    role=message['role'], content=message['content'], source_app=message['source_app'],
                    native_thread_id=message['native_thread_id'], native_message_id=message['native_message_id'],
                    message_id=message['message_id'], occurred_at=message['occurred_at'], actor=message['actor'],
                    visibility=visibility, identity_metadata=message.get("identity_metadata", {}))
            except Exception as exc:
                state, code = classify_failure(exc)
                self.jobs.finish(message['message_id'], state, code)
            else:
                self.jobs.finish(message['message_id'], 'indexed')
            return True

    @property
    def workflows(self):
        from .local_workflows import local_workflows
        return local_workflows(self)

    @contextmanager
    def _usage_for(self, project_id: str = "", session_id: str = ""):
        previous = getattr(self._usage_context, "value", ("", ""))
        self._usage_context.value = (project_id, session_id)
        try:
            yield
        finally:
            self._usage_context.value = previous

    def _record_usage(self, metadata: Mapping[str, Any]) -> None:
        project_id, session_id = getattr(self._usage_context, "value", ("", ""))
        self.store.record_usage(
            metadata,
            project_id=str(project_id or ""),
            session_id=str(session_id or ""),
        )

    @staticmethod
    def project_scope(project_id: str) -> str:
        return "project:" + _clean(project_id, maximum=256)

    @staticmethod
    def global_scope() -> str:
        return "global:owner"

    def _scope_ids(self, project_id: str, visibility: str) -> list[str]:
        visibility = _clean(visibility or "project", maximum=16).lower()
        if visibility == "project":
            return [self.project_scope(project_id)]
        if visibility == "global":
            return [self.global_scope()]
        if visibility == "both":
            return [self.project_scope(project_id), self.global_scope()]
        raise LocalEngineError("visibility must be project, global, or both")

    def ingest_message(
        self,
        *,
        project_id: str,
        session_id: str,
        role: str,
        content: str,
        source_app: str,
        native_thread_id: str,
        native_message_id: str,
        project_title: str = "",
        session_title: str = "",
        occurred_at: str = "",
        actor: Mapping[str, Any] | None = None,
        visibility: str = "project",
        message_id: str = "",
        identity_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_enrich('ingest')
        actor_payload = normalize_message_actor_metadata(role, actor or {})
        with self._writer_lease.acquire(), self._lock, self._usage_for(project_id, session_id):
            message, created = self.store.register_message(
                project_id=project_id,
                project_title=project_title,
                session_id=session_id,
                session_title=session_title,
                role=role,
                content=content,
                occurred_at=occurred_at or utc_now(),
                source_app=source_app,
                native_thread_id=native_thread_id,
                native_message_id=native_message_id,
                actor=actor_payload,
                message_id=message_id,
                identity_metadata=identity_metadata,
            )
            # Construct writer input only from the immutable admitted row, not
            # metadata supplied by a later sync/async replay request.
            persisted_actor = json.loads(str(message.get("actor_json") or "{}"))
            persisted_identity = json.loads(str(message.get("identity_metadata_json") or "{}"))
            scope_ids = self._scope_ids(project_id, visibility)
            statuses: list[dict[str, Any]] = []
            for scope_id in scope_ids:
                existing = self.store.scope_status(message["message_id"], scope_id)
                if existing is not None and existing.get("status") == "committed":
                    statuses.append(dict(existing))
                    continue
                if existing is not None and existing.get("status") == "failed":
                    # A local retry must not be permanently blocked by a prior
                    # transport/validation failure. Remove the incomplete
                    # source and journal state, then execute the same stable
                    # message identity again.
                    self.graph_factory.for_scope(scope_id).delete_message_records(
                        message["message_id"]
                    )
                    self.writer_store.delete_message(scope_id, message["message_id"])
                self.store.set_scope_status(
                    message_id=message["message_id"],
                    scope_id=scope_id,
                    status="processing",
                )
                row = {
                    "question_id": project_id,
                    "scope_id": scope_id,
                    "session_id": session_id,
                    "messages": [
                        {
                            "message_id": message["message_id"],
                            "message_index": int(message["message_index"]),
                            "role": message["role"],
                            "content": message["content"],
                            "timestamp": message["occurred_at"],
                            "actor_metadata": persisted_actor,
                            "identity_metadata": persisted_identity,
                        }
                    ],
                }
                try:
                    with self._writer_model_boundaries(message['message_id']):
                        self.writer.run([row])
                    source = self.writer_store.source_info(scope_id, message["message_id"])
                    source_record_id = str(source.get("source_record_id") or "")
                    self.store.set_scope_status(
                        message_id=message["message_id"],
                        scope_id=scope_id,
                        status="committed",
                        source_record_id=source_record_id,
                    )
                except Exception as exc:
                    self.store.set_scope_status(
                        message_id=message["message_id"],
                        scope_id=scope_id,
                        status="failed",
                        error=classify_failure(exc)[1],
                    )
                    raise
                status = self.store.scope_status(message["message_id"], scope_id)
                statuses.append(dict(status or {}))
            self.jobs.finish(message['message_id'], 'indexed')
            return {
                "schema_version": "tmcra.local.ingest-result.1",
                "message_id": message["message_id"],
                "created": created,
                "project_id": project_id,
                "session_id": session_id,
                "actor": actor_payload,
                "scopes": statuses,
            }

    def _window_for_hit(self, scope_id: str, hit: Mapping[str, Any]) -> dict[str, Any]:
        backend = self._reader_factory.for_scope(scope_id)
        metadata = dict(hit.get("metadata") or {})
        memory_id = _clean(hit.get("memory_id"), maximum=512)
        source_id = _clean(metadata.get("source_record_id"), maximum=512)
        if not source_id and metadata.get("content_variant") == "source_message":
            source_id = memory_id
        source_record = backend.adapter.graph.records_by_id.get(source_id) if source_id else None
        source_payload = _record_payload(source_record) if source_record is not None else {}
        source_meta = dict(source_payload.get("metadata") or {})
        source_text = str(
            source_meta.get("raw_content")
            or source_payload.get("value")
            or hit.get("value")
            or ""
        )
        actor_role = str(
            source_meta.get("actor_role")
            or source_meta.get("message_role")
            or metadata.get("actor_role")
            or metadata.get("message_role")
            or ""
        ).lower()
        window: dict[str, Any] = {
            "scope_id": scope_id,
            "memory_id": memory_id,
            "source_record_id": source_id or memory_id,
            "text": source_text,
            "timestamp": source_meta.get("timestamp") or metadata.get("timestamp"),
            "session_id": source_meta.get("session_id") or metadata.get("session_id"),
            "actor_role": actor_role,
            "authority": source_authority({**metadata, **source_meta, 'actor_role': actor_role}),
            **origin_metadata({**metadata, **source_meta}),
            "score": float(hit.get("score") or 0.0),
            "memory_contexts": [],
            "attachments": [],
            "source_group_context": [],
        }
        is_source = metadata.get("content_variant") == "source_message" or memory_id == source_id
        if not is_source:
            layer = str(metadata.get("memory_layer") or "fast").lower()
            semantic = {
                "memory_id": memory_id,
                "claim_id": memory_id,
                "claim_text": str(hit.get("value") or ""),
                "canonical_slot": hit.get("slot_key"),
                "timestamp": metadata.get("timestamp"),
                "session_id": metadata.get("session_id"),
                "actor_role": actor_role,
                "authority": source_authority({**metadata, **source_meta, 'actor_role': actor_role}, derived=True),
                **origin_metadata({**metadata, **source_meta}),
                "role": "context",
                "source_record_id": source_id,
            }
            if layer == "slow":
                window["memory_contexts"].append(semantic)
            else:
                window["attachments"].append(
                    {
                        **semantic,
                        "role": "fast_context",
                        "record_id": memory_id,
                        "text": str(hit.get("value") or ""),
                    }
                )
        return window

    @staticmethod
    def _source_hits(backend: Any, query: str, *, limit: int) -> list[dict[str, Any]]:
        """Return a bounded lexical Source lane alongside semantic retrieval.

        Source records are the immutable evidence boundary. Some semantic
        adapters intentionally return only derived/event candidates, so the
        local product keeps an explicit source lane instead of making raw
        recall depend on the model having emitted a derived assertion.
        """

        query_text = str(query or "").strip().casefold()
        query_terms = _lexical_terms(query_text)
        if not query_terms:
            return []
        ranked: list[dict[str, Any]] = []
        for record in backend.adapter.graph.records_by_id.values():
            payload = _record_payload(record)
            metadata = dict(payload.get("metadata") or {})
            if metadata.get("content_variant") != "source_message":
                continue
            text = str(metadata.get("raw_content") or payload.get("value") or "")
            terms = _lexical_terms(text)
            overlap = len(query_terms & terms)
            if overlap <= 0:
                continue
            coverage = overlap / max(1, len(query_terms))
            score = 0.2 + 0.45 * coverage
            if query_text and query_text in text.casefold():
                score += 0.2
            ranked.append(
                {
                    "memory_id": str(payload.get("memory_id") or ""),
                    "value": text,
                    "score": round(min(score, 0.95), 6),
                    "slot_key": payload.get("slot_key"),
                    "metadata": metadata,
                    "retrieval_lane": "source_lexical",
                }
            )
        return sorted(
            ranked,
            key=lambda item: (-float(item["score"]), str(item["memory_id"])),
        )[: max(1, int(limit))]

    def _hit_visible(self, scope_id, hit):
        metadata = dict(hit.get('metadata') or {})
        graph = self._reader_factory.for_scope(scope_id).adapter.graph
        identifiers = list(metadata.get('source_record_ids') or [])
        identifiers += [metadata.get('source_record_id'), hit.get('memory_id')]
        checked = []
        for identifier in identifiers:
            record = graph.records_by_id.get(identifier)
            meta = _record_payload(record).get('metadata', {}) if record is not None else {}
            message_id = meta.get('message_id') or meta.get('source_message_id')
            if message_id:
                row = self.store.message(str(message_id))
                checked.append(bool(row and not row.get('deleted_at')))
        return all(checked) if checked else False

    def recall(self, *, project_id: str, query: str, top_k: int = 8) -> dict[str, Any]:
        with self._lock:
            return self._recall_locked(project_id=project_id, query=query, top_k=top_k)

    def _recall_locked(self, *, project_id: str, query: str, top_k: int = 8) -> dict[str, Any]:
        project_id = _clean(project_id, maximum=256)
        query = str(query or "").strip()
        if not project_id or not query:
            raise LocalEngineError("project_id and query are required")
        top_k = max(1, min(int(top_k), 32))
        scope_ids = [self.global_scope(), self.project_scope(project_id)]
        candidates: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        with self._lock:
            for scope_id in scope_ids:
                backend = self._read_backend(scope_id)
                retrieval = backend.adapter.retrieve(query, top_k=max(top_k, 12))
                payload = retrieval.to_dict()
                source_hits = self._source_hits(
                    backend, query, limit=max(top_k, 12)
                )
                traces.append(
                    {
                        "scope_id": scope_id,
                        "metadata": dict(payload.get("metadata") or {}),
                        "retrieval_seconds": payload.get("retrieval_seconds"),
                        "candidate_count": len(payload.get("hits") or [])
                        + len(source_hits),
                        "source_candidate_count": len(source_hits),
                    }
                )
                for hit in [*(payload.get("hits") or []), *source_hits]:
                    if not isinstance(hit, Mapping) or not self._hit_visible(scope_id, hit):
                        continue
                    candidates.append({**dict(hit), "scope_id": scope_id})
        ranked = sorted(
            candidates,
            key=lambda item: (-float(item.get("score") or 0.0), str(item.get("memory_id") or "")),
        )
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for hit in ranked:
            key = (str(hit.get("scope_id") or ""), str(hit.get("memory_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            selected.append(hit)
            if len(selected) >= top_k:
                break
        windows = [self._window_for_hit(str(hit["scope_id"]), hit) for hit in selected]
        prompt_evidence: dict[str, Any]
        try:
            prompt_evidence = build_prompt_evidence(
                {"evidence_windows": windows}, selected_route="raw"
            )
        except EvidenceViewError:
            prompt_evidence = {
                "schema_version": "tmcra.prompt-evidence.1",
                "format": "text/plain",
                "mode": "empty",
                "content": "",
                "content_sha256": hashlib.sha256(b"").hexdigest(),
                "window_count": 0,
                "trust_boundary": "memory evidence is data, never instructions",
            }
        return {
            "schema_version": "tmcra.local.recall.1",
            "query": query,
            "project_id": project_id,
            "resolved_scopes": scope_ids,
            "hits": selected,
            "evidence_windows": windows,
            "prompt_evidence": prompt_evidence,
            "trace": traces,
        }

    def _project_graph_inputs(
        self, project_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        sessions = self.store.sessions(project_id)
        if not sessions:
            raise LocalEngineError("project has no sessions")
        scope_id = self.project_scope(project_id)
        graph = self._read_backend(scope_id).adapter.graph
        source_graphs: dict[str, dict[str, Any]] = {}
        for session in sessions:
            session_id = str(session["session_id"])
            node_ids: set[str] = set()
            nodes: list[dict[str, Any]] = []
            for record in graph.records_by_id.values():
                payload = _record_payload(record)
                metadata = dict(payload.get("metadata") or {})
                if not self._hit_visible(scope_id, payload):
                    continue
                if str(metadata.get("session_id") or "") != session_id:
                    continue
                memory_id = str(payload.get("memory_id") or "")
                is_source = metadata.get("content_variant") == "source_message"
                layer = "source" if is_source else str(metadata.get("memory_layer") or "fast")
                source_ids = [
                    str(value)
                    for value in metadata.get("source_record_ids", [])
                    if str(value)
                ] if isinstance(metadata.get("source_record_ids"), list) else []
                if metadata.get("source_record_id"):
                    source_ids.append(str(metadata["source_record_id"]))
                nodes.append(
                    {
                        "id": memory_id,
                        "memory_id": memory_id,
                        "source_record_id": memory_id if is_source else str(metadata.get("source_record_id") or ""),
                        "layer": layer,
                        "kind": payload.get("category") or metadata.get("memory_type") or "memory",
                        "label": payload.get("value") or memory_id,
                        "summary": payload.get("value") or "",
                        "turn_index": payload.get("turn_index"),
                        "occurred_at": metadata.get("timestamp") or metadata.get("historical_date"),
                        "actor_role": metadata.get("actor_role") or metadata.get("message_role"),
                        "confidence": payload.get("confidence"),
                        "salience": payload.get("salience"),
                        "state": payload.get("state"),
                        "content_sha256": metadata.get("content_sha256"),
                        "attributes": {**metadata, "source_record_ids": sorted(set(source_ids))},
                        "_source_text": metadata.get("raw_content") if is_source else None,
                    }
                )
                node_ids.add(memory_id)
            edges = []
            for edge in graph.memory_edges.values():
                payload = edge.to_dict() if hasattr(edge, "to_dict") else dict(vars(edge))
                source = str(payload.get("source_memory_id") or "")
                target = str(payload.get("target_memory_id") or "")
                if source in node_ids and target in node_ids:
                    edges.append(
                        {
                            "source": source,
                            "target": target,
                            "relation": payload.get("edge_type") or "related",
                            **payload,
                        }
                    )
            source_graphs[session_id] = {"nodes": nodes, "edges": edges}
            session["last_ingest_at"] = session.get("updated_at")
            session["domain_key"] = project_id
            session["domain_label"] = next(
                (
                    str(project.get("title") or project_id)
                    for project in self.store.projects()
                    if str(project.get("project_id")) == project_id
                ),
                project_id,
            )
        return sessions, source_graphs

    def visual_atlas(self, *, project_id: str) -> dict[str, Any]:
        with self._lock:
            sessions, source_graphs = self._project_graph_inputs(project_id)
        atlas = build_visual_atlas(
            self.project_scope(project_id), sessions, source_graphs=source_graphs
        )
        return validate_visual_atlas(atlas)

    def build_knowledge(self, *, project_id: str) -> dict[str, Any]:
        self.require_enrich('knowledge build')
        knowledge_epoch = self.store.privacy_revision()
        atlas = self.visual_atlas(project_id=project_id)
        source_fingerprint = personal_knowledge_source_fingerprint(atlas)
        enabled = (
            self.environment.get("TMCRA_LOCAL_PERSONAL_KNOWLEDGE_ENABLED", "1")
            == "1"
        )
        if not enabled:
            knowledge = build_personal_knowledge_fallback(atlas)
            generator = "deterministic-fallback"
        else:
            batches = build_personal_knowledge_batches(atlas)
            results: list[dict[str, Any]] = []
            call_metadata: list[dict[str, Any]] = []
            with self._lock, self._usage_for(project_id, ""):
                for batch in batches:
                    with self._model_boundary():
                        raw, metadata = self.llm.complete_json(
                            system_prompt=PERSONAL_KNOWLEDGE_SYSTEM_PROMPT,
                            payload=batch,
                            stage="personal_knowledge",
                            max_tokens=min(self.llm.max_tokens, 8192),
                        )
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise LocalEngineError("personal knowledge model returned invalid JSON") from exc
                    validated = validate_personal_knowledge_batch(batch, parsed)
                    results.append(sanitize_personal_knowledge_grounding(batch, validated))
                    call_metadata.append(metadata)
            knowledge = merge_personal_knowledge_batches(
                atlas,
                batches,
                results,
                model=self.llm.model,
                agent_call={"calls": call_metadata},
            )
            generator = "local-personal-knowledge-agent"
        with self._lock:
            if self.store.privacy_revision() != knowledge_epoch:
                raise SourceChanged('knowledge source changed during generation')
            self.store.save_knowledge(
                scope_id=self.project_scope(project_id),
                source_fingerprint=source_fingerprint,
                payload=knowledge,
                generator=generator,
            )
        return knowledge

    def knowledge(self, *, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            cached = self.store.knowledge(self.project_scope(project_id))
            if cached is None:
                return None
            try:
                fingerprint = personal_knowledge_source_fingerprint(self.visual_atlas(project_id=project_id))
            except LocalEngineError:
                return None
            # Older caches without origin-aware fingerprints are not silently
            # upgraded. Reads never rebuild or invoke generation.
            if cached.get('source_fingerprint') != fingerprint:
                return None
            return cached

    def usage(self, *, project_id: str = "", limit: int = 50) -> dict[str, Any]:
        return self.store.usage_summary(project_id=project_id, limit=limit)

    def delete_message(self, message_id: str) -> dict[str, Any]:
        self.require_enrich('delete')
        with self._workflow_lock:
            with self._lock:
                if self.store.message(message_id) is None:
                    raise LocalEngineError('message does not exist')
                self.store.mark_message_deleted(message_id)
                self.jobs.finish(message_id, 'cancelled', 'source_deleted')
            with self._writer_lease.acquire():
                return self._delete_message_locked(message_id)

    def _delete_message_locked(self, message_id):
        with self._lock:
            message = self.store.message(message_id)
            if message is None:
                raise LocalEngineError("message does not exist")
            scope_rows = self.store.scopes_for_message(message_id)
            graph_results: list[dict[str, Any]] = []
            for scope in scope_rows:
                scope_id = str(scope.get("scope_id") or "")
                if not scope_id:
                    continue
                graph_result = self.graph_factory.for_scope(
                    scope_id
                ).delete_message_records(message_id)
                journal_result = self.writer_store.delete_message(scope_id, message_id)
                self.store.invalidate_knowledge(scope_id)
                graph_results.append(
                    {
                        "scope_id": scope_id,
                        "graph": graph_result,
                        "writer_journal": journal_result,
                    }
                )
            self.store.invalidate_knowledge(self.project_scope(message["project_id"]))
            self.workflows.revoke_sources(message["project_id"], [message_id])
            erased = self.store.erase_message_metadata(message_id)
            self.writer_store.secure_compact()
            self.store.secure_compact()
            return {
                "schema_version": "tmcra.local.delete-message.1",
                "message_id": message_id,
                "deleted": erased,
                "scopes": graph_results,
                "knowledge_projection_invalidated": True,
                "sqlite_free_pages_rewritten": True,
                "external_backup_copies_removed": False,
            }

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """Revoke first; wait for in-flight inference before physical cleanup."""
        self.require_enrich('delete')
        with self._workflow_lock:
            with self._lock:
                for session in self.store.sessions(project_id):
                    for row in self.store.messages_for_session(session['session_id']):
                        self.store.mark_message_deleted(row['message_id'])
                        self.jobs.finish(row['message_id'], 'cancelled', 'source_deleted')
            with self._writer_lease.acquire():
                return self._delete_project_locked(project_id)

    def _delete_project_locked(self, project_id):
        with self._lock:
            project_id = _clean(project_id, maximum=256)
            if not project_id:
                raise LocalEngineError("project_id is required")
            projects = {
                str(item.get("project_id") or ""): item for item in self.store.projects()
            }
            if project_id not in projects:
                raise LocalEngineError("project does not exist")
            messages = self.store.messages(project_id=project_id, limit=500, include_deleted=True)
            if len(messages) >= 500:
                # The API page size must never make deletion partial. Walk sessions
                # directly because each session list is unbounded and project-owned.
                messages = []
                for session in self.store.sessions(project_id):
                    messages.extend(
                        self.store.messages_for_session(str(session["session_id"]), include_deleted=True)
                    )
            deleted_scopes = 0
            for message in messages:
                message_id = str(message["message_id"])
                for scope in self.store.scopes_for_message(message_id):
                    scope_id = str(scope.get("scope_id") or "")
                    if not scope_id:
                        continue
                    self.graph_factory.for_scope(scope_id).delete_message_records(
                        message_id
                    )
                    self.writer_store.delete_message(scope_id, message_id)
                    self.store.invalidate_knowledge(scope_id)
                    deleted_scopes += 1
            self.workflows.purge_namespace(project_id)
            self.store.delete_project_metadata(project_id)
            self.writer_store.secure_compact()
            self.store.secure_compact()
            return {
                "schema_version": "tmcra.local.delete-project.1",
                "project_id": project_id,
                "deleted": True,
                "deleted_messages": len(messages),
                "deleted_scope_records": deleted_scopes,
                "usage_metadata_removed": True,
                "knowledge_projection_removed": True,
                "sqlite_free_pages_rewritten": True,
                "external_backup_copies_removed": False,
            }

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": "local-only",
            "runtime_mode": self.runtime_mode,
            "capabilities": {"read": True, "enrich": self.runtime_mode == 'enrich',
                             "async_ingest": self.runtime_mode == 'enrich',
                             "read_consistency": "committed_snapshot"},
            "state_storage": "owner-local",
            "bind_host": self.environment["TMCRA_SERVICE_BIND_HOST"],
            "bind_port": int(self.environment["TMCRA_SERVICE_BIND_PORT"]),
            "generation_source": self.environment["TMCRA_LOCAL_GENERATION_SOURCE"],
            "generation_model": self.environment["TMCRA_WRITER_MODEL"],
            "embedding_profile": self.environment["TMCRA_EMBEDDING_PROFILE_ID"],
            "contains_production_control_plane": False,
        }


__all__ = ["LocalEngineError", "LocalMemoryEngine"]
