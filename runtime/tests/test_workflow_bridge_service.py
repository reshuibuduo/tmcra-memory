"""Real sidecars with mock ordinary-engine callbacks, without models/network."""
import copy
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tmcra_local.memory_bridge import BridgeError
from tmcra_local.workflow_service import WorkflowService


@pytest.fixture
def records():
    return {"source": {"m1": {"id": "m1", "message_id": "m1", "kind": "source", "text": "Context", "role": "user",
                               "occurred_at": "2026-10-15T10:00:00Z", "title": "Selected context"}}, "target": {}}


def service(tmp_path, records, **callbacks):
    return WorkflowService(tmp_path, lambda ns, id: records.get(ns, {}).get(id),
                           lambda ns: [dict(r) for r in records.get(ns, {}).values()], **callbacks)


def run(svc, namespace, action, payload):
    return svc.execute(namespace, action, payload, "operation-test")


def source_package(svc):
    return run(svc, "source", "bridge.export", {"selected_ids": ["m1"], "mode": "selected_sources"})


def preview(svc, package):
    return run(svc, "target", "bridge.preview", {"package": package, "selected_ids": [package["items"][0]["id"]]})


def test_bridge_fields_closed_default_selection_empty_and_scope_resolution(tmp_path, records):
    svc = service(tmp_path, records)
    with pytest.raises(BridgeError):
        run(svc, "source", "bridge.export", {})
    with pytest.raises(BridgeError, match="Unknown"):
        run(svc, "target", "bridge.preview", {"package": source_package(svc), "selected_ids": ["m1"], "external_target_revision": "fake"})
    with pytest.raises(BridgeError, match="Unknown"):
        run(svc, "target", "bridge.commit", {"preview_token": "fake", "records": ["injected"]})
    with pytest.raises(BridgeError, match="inaccessible"):
        run(svc, "target", "bridge.export", {"selected_ids": ["m1"], "mode": "selected_sources"})
    with pytest.raises(BridgeError, match="inaccessible"):
        run(svc, "source", "bridge.export_revocations", {"selected_ids": ["file:///etc/passwd"]})


def test_import_calls_ordinary_writer_once_and_keeps_provenance_unverified(tmp_path, records):
    calls = []
    def importer(ns, entry):
        calls.append((ns, copy.deepcopy(entry)))
        return {"local_message_id": "imported-1", "indexed": True}
    svc = service(tmp_path, records, importer=importer)
    pkg = source_package(svc)
    approved = preview(svc, pkg)
    result = run(svc, "target", "bridge.commit", {"preview_token": approved["preview_token"]})
    assert result["indexed"] and result["status"] == "applied"
    assert calls[0][1]["record"]["source"] == pkg["items"][0]["source"]
    assert calls[0][1]["record"]["verification"] == "external_unverified"
    assert run(svc, "target", "bridge.commit", {"preview_token": approved["preview_token"]}) == result
    assert len(calls) == 1


def test_async_writer_stays_pending_then_receipt_probe_indexes_without_resubmission(tmp_path, records):
    calls, completed = [], [False]
    def importer(ns, entry):
        calls.append(entry)
        return {"local_message_id": "imported-1", "receipt_id": "job-1", "indexed": False}
    def status(ns, receipt):
        return {**receipt, "indexed": completed[0]}
    svc = service(tmp_path, records, importer=importer, receipt_resolver=status)
    p = preview(svc, source_package(svc))
    result = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert result["status"] == "accepted_pending" and not result["indexed"]
    again = run(svc, "target", "bridge.plan", {"plan_id": result["plan_id"]})
    assert not again["indexed"] and len(calls) == 1
    completed[0] = True
    final = run(svc, "target", "bridge.plan", {"plan_id": result["plan_id"]})
    assert final["indexed"] and len(calls) == 1


def test_source_edit_invalidates_preview_before_writer_called(tmp_path, records):
    calls = []
    svc = service(tmp_path, records, importer=lambda ns, entry: calls.append(entry))
    p = preview(svc, source_package(svc))
    records["target"]["new"] = {"id": "new", "text": "target changed"}
    with pytest.raises(BridgeError, match="external target revision"):
        run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert calls == []


def test_common_secret_pattern_requires_explicit_opt_in_even_without_client_label(tmp_path, records):
    records["source"]["m1"]["text"] = "api_key=not-a-real-secret-but-sensitive"
    svc = service(tmp_path, records)
    with pytest.raises(BridgeError, match="sensitive"):
        source_package(svc)
    pkg = run(svc, "source", "bridge.export", {"selected_ids": ["m1"], "mode": "selected_sources", "allow_sensitive_fields": ["text"]})
    assert pkg["items"][0]["sensitive_fields"] == ["text"]
    assert not preview(svc, pkg)["can_commit"]


def test_revocation_deletes_approved_targets_only_and_pending_delete_retries(tmp_path, records):
    imports, deletes, finished = [], [], [False]
    def importer(ns, entry):
        imports.append(entry)
        return {"local_message_id": "imported-1", "indexed": True}
    def deleter(ns, entry):
        deletes.append(entry)
        return {"deleted_message_ids": ["imported-1"] if finished[0] else [], "pending": [] if finished[0] else ["job-delete"]}
    svc = service(tmp_path, records, importer=importer, deleter=deleter)
    normal = source_package(svc)
    p = preview(svc, normal)
    run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    revoke_package = run(svc, "source", "bridge.export_revocations", {"selected_ids": ["m1"]})
    p = preview(svc, revoke_package)
    assert p["revocations"][0]["targets"][0]["local_message_id"] == "imported-1"
    pending = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert pending["status"] == "planned" and not pending["indexed"]
    assert deletes[-1]["revoked_local_ids"] == ["imported-1"]
    assert len(imports) == 1
    finished[0] = True
    done = run(svc, "target", "bridge.plan", {"plan_id": pending["plan_id"]})
    assert done["status"] == "applied" and not done["indexed"]
    run(svc, "target", "bridge.plan", {"plan_id": done["plan_id"]})
    assert len(deletes) == 2
    assert not preview(svc, normal)["can_commit"]


def test_deleted_source_can_export_identity_only_revocation_and_survives_restart(tmp_path, records):
    svc = service(tmp_path, records)
    pkg = source_package(svc)
    original_identity = pkg["items"][0]["source"]
    svc.revoke_sources("source", ["m1"])
    records["source"].clear()
    reopened = service(tmp_path, records)
    assert reopened.system == svc.system
    deleted = reopened.snapshot("source")["available_revocations"]
    assert len(deleted) == 1 and deleted[0]["source"] == original_identity
    revocation = run(reopened, "source", "bridge.export_revocations", {"selected_ids": [deleted[0]["id"]]})
    assert revocation["items"][0]["text"] == ""
    assert revocation["items"][0]["source"] == original_identity
    assert "Context" not in str(revocation)
    assert reopened.snapshot("target")["available_revocations"] == []


def test_relay_export_retains_foreign_source_and_deletion_revokes_foreign_origin(tmp_path, records):
    svc = service(tmp_path, records, importer=lambda ns, entry: {"local_message_id": "new-local", "indexed": True})
    pkg = source_package(svc)
    p = preview(svc, pkg)
    run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    records["target"]["new-local"] = {**records["source"]["m1"], "id": "new-local", "message_id": "new-local"}
    relay = run(svc, "target", "bridge.export", {"selected_ids": ["new-local"], "mode": "selected_sources"})
    assert relay["items"][0]["source"] == pkg["items"][0]["source"]
    assert relay["items"][0]["item_sha256"] == pkg["items"][0]["item_sha256"]
    svc.revoke_sources("target", ["new-local"])
    records["target"].clear()
    assert not preview(svc, pkg)["can_commit"]
    origins = [r["source"] for r in svc.snapshot("target")["available_revocations"]]
    assert pkg["items"][0]["source"] in origins


def test_adapter_failure_does_not_report_success_or_echo_private_error(tmp_path, records):
    def importer(ns, entry):
        raise RuntimeError("secret key = never-echo-this")
    svc = service(tmp_path, records, importer=importer)
    p = preview(svc, source_package(svc))
    result = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert result["status"] == "planned" and not result["indexed"]
    assert result["errors"] == [{"item_id": "m1", "error": "RuntimeError", "retryable": True}]
    assert "never-echo" not in str(result)


def make_decision(svc, identifier, **extra):
    return svc.execute("source", "decision.create", {"id": identifier, "title": identifier,
                       "text": "private-derived-content-" + identifier, **extra}, "create-" + identifier)


def test_delete_exported_decision_adds_origin_fence_and_content_free_revocation(tmp_path, records):
    svc = service(tmp_path, records)
    make_decision(svc, "d1", evidence_ids=["m1"])
    pkg = run(svc, "source", "bridge.export", {"selected_ids": ["decision:d1"], "mode": "knowledge_only"})
    svc.execute("source", "record.delete", {"kind": "decision", "id": "d1"}, "delete-d1")
    revocations = svc.snapshot("source")["available_revocations"]
    assert any(r["source"] == pkg["items"][0]["source"] for r in revocations)
    blocked = run(svc, "source", "bridge.preview", {"package": pkg, "selected_ids": ["decision:d1"]})
    assert not blocked["can_commit"]
    with sqlite3.connect(svc.root / "identity.sqlite3") as db:
        assert "private-derived-content" not in str(db.execute("SELECT * FROM bridge_deleted_origins").fetchall())
        assert db.execute("SELECT count(*) FROM bridge_exported_origins").fetchone()[0] == 0


def test_source_revocation_cascades_exported_knowledge_dependencies_and_purge(tmp_path, records):
    svc = service(tmp_path, records)
    make_decision(svc, "d1", evidence_ids=["m1"])
    make_decision(svc, "d2", depends_on=["d1"])
    pkg = run(svc, "source", "bridge.export", {"selected_ids": ["decision:d1", "decision:d2"], "mode": "knowledge_only"})
    svc.revoke_sources("source", ["m1"])
    ids = {r["source"]["id"] for r in svc.snapshot("source")["available_revocations"]}
    assert {"m1", "decision:d1", "decision:d2"} <= ids
    assert svc.core.snapshot("source")["decisions"] == []
    assert not run(svc, "source", "bridge.preview", {"package": pkg, "selected_ids": ["decision:d2"]})["can_commit"]
    assert svc.snapshot("target")["available_revocations"] == []
    svc.purge_namespace("source")
    assert b"private-derived-content" not in (svc.root / "identity.sqlite3").read_bytes()


def test_export_dependency_union_covers_prior_decision_evidence_after_revise(tmp_path, records):
    svc = service(tmp_path, records)
    make_decision(svc, "d1", evidence_ids=["m1"])
    run(svc, "source", "bridge.export", {"selected_ids": ["decision:d1"], "mode": "knowledge_only"})
    svc.execute("source", "decision.revise", {"id": "d1", "text": "new without explicit source", "reason": "new basis", "evidence_ids": []}, "revise-d1")
    run(svc, "source", "bridge.export", {"selected_ids": ["decision:d1"], "mode": "knowledge_only"})
    svc.revoke_sources("source", ["m1"])
    assert "decision:d1" in {r["source"]["id"] for r in svc.revocation_records("source")}


def test_revision_callback_covers_source_beyond_ui_listing(tmp_path, records):
    revision = [1]
    svc = service(tmp_path, records, revision_resolver=lambda ns: revision[0])
    p = preview(svc, source_package(svc))
    revision[0] = 2
    with pytest.raises(BridgeError, match="external target revision"):
        run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})


def test_shared_engine_lock_serializes_delete_against_import_commit(tmp_path, records):
    engine_lock = threading.RLock()
    entered, release = threading.Event(), threading.Event()
    def importer(ns, entry):
        with engine_lock:
            entered.set()
            assert release.wait(3)
            return {"local_message_id": "imported-1", "indexed": True}
    svc = service(tmp_path, records, importer=importer, operation_lock=engine_lock)
    pkg = source_package(svc)
    p = preview(svc, pkg)
    with ThreadPoolExecutor(max_workers=2) as pool:
        importing = pool.submit(run, svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
        assert entered.wait(3)
        deleting = pool.submit(svc.revoke_sources, "target", ["imported-1"])
        release.set()
        importing.result(timeout=5)
        deleting.result(timeout=5)
    assert not preview(svc, pkg)["can_commit"]
    assert run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})["status"] == "revoked"


def test_delete_first_invalidates_existing_import_approval(tmp_path, records):
    calls = []
    svc = service(tmp_path, records, importer=lambda ns, entry: calls.append(entry))
    pkg = source_package(svc)
    p = run(svc, "source", "bridge.preview", {"package": pkg, "selected_ids": ["m1"]})
    svc.revoke_sources("source", ["m1"])
    with pytest.raises(BridgeError, match="revoked"):
        run(svc, "source", "bridge.commit", {"preview_token": p["preview_token"]})
    assert calls == []


@pytest.mark.parametrize("state,code", [("failed", "ingest_failed"), ("cancelled", "ingest_cancelled")])
def test_terminal_async_failure_is_observed_without_resubmission_or_secret_error(tmp_path, records, state, code):
    calls, current_state = [], ["running"]
    def importer(ns, entry):
        calls.append(entry)
        return {"local_message_id": "imported-1", "receipt_id": "job-1", "indexed": False}
    def status(ns, receipt):
        return {**receipt, "engine_state": current_state[0], "error": "private-provider-key-never-show"}
    svc = service(tmp_path, records, importer=importer, receipt_resolver=status)
    p = preview(svc, source_package(svc))
    pending = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert pending["status"] == "accepted_pending"
    current_state[0] = state
    revision = svc.bridge.revision("target")
    readonly = svc.snapshot("target")["bridge_plans"][0]
    assert readonly["status"] == "failed" and not readonly["indexed"]
    assert readonly["errors"] == [{"item_id": "m1", "error": code, "retryable": False}]
    assert "private-provider-key" not in str(readonly)
    assert svc.bridge.revision("target") == revision
    assert svc.bridge.get_plan("target", pending["plan_id"])["status"] == "accepted_pending"
    observed = run(svc, "target", "bridge.plan", {"plan_id": pending["plan_id"]})
    assert observed["status"] == "failed" and observed["errors"] == readonly["errors"]
    assert len(calls) == 1


def test_readonly_job_success_observation_does_not_mutate_sidecar_or_submit(tmp_path, records):
    calls, ready = [], [False]
    def importer(ns, entry):
        calls.append(entry)
        return {"local_message_id": "imported-1", "receipt_id": "job-1", "indexed": False}
    svc = service(tmp_path, records, importer=importer,
                  receipt_resolver=lambda ns, receipt: {**receipt, "indexed": ready[0], "engine_state": "succeeded" if ready[0] else "queued"})
    p = preview(svc, source_package(svc))
    pending = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    revision = svc.bridge.revision("target")
    ready[0] = True
    observed = svc.snapshot("target")["bridge_plans"][0]
    assert observed["indexed"] and observed["status"] == "applied"
    assert observed["stored_status"] == "accepted_pending"
    assert svc.bridge.revision("target") == revision
    assert svc.bridge.get_plan("target", pending["plan_id"])["status"] == "accepted_pending"
    final = run(svc, "target", "bridge.plan", {"plan_id": pending["plan_id"]})
    assert final["indexed"] and final["errors"] == [] and len(calls) == 1


def test_receipt_identity_mismatch_does_not_promote_indexed_status(tmp_path, records):
    svc = service(tmp_path, records,
                  importer=lambda ns, entry: {"local_message_id": "imported-1", "receipt_id": "job-1", "indexed": False},
                  receipt_resolver=lambda ns, receipt: {**receipt, "receipt_id": "wrong-job", "indexed": True})
    p = preview(svc, source_package(svc))
    result = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert not result["indexed"]
    assert result["errors"] == [{"item_id": "m1", "error": "ingest_receipt_invalid", "retryable": False}]


def test_revocation_permission_rechecked_from_server_approval_before_fence(tmp_path, records):
    granted, callbacks = [False], []
    def authorization(ns, normalized_records):
        callbacks.append((ns, copy.deepcopy(normalized_records)))
        if any(r["kind"] == "revocation" for r in normalized_records) and not granted[0]:
            raise PermissionError("delete permission required")
    svc = service(tmp_path, records, authorize_records=authorization,
                  importer=lambda ns, entry: {"local_message_id": "imported-1", "indexed": True},
                  deleter=lambda ns, entry: {"deleted_message_ids": entry["revoked_local_ids"], "pending": []})
    normal = source_package(svc)
    p = preview(svc, normal)
    imported = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    revoked = run(svc, "source", "bridge.export_revocations", {"selected_ids": ["m1"]})
    revision = svc.bridge.revision("target")
    with pytest.raises(PermissionError):
        preview(svc, revoked)
    assert svc.bridge.revision("target") == revision
    granted[0] = True
    p = preview(svc, revoked)
    granted[0] = False
    with pytest.raises(PermissionError):
        run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert svc.bridge.get_plan("target", imported["plan_id"])["status"] == "applied"
    assert svc.bridge.revision("target") == revision
    granted[0] = True
    done = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert done["status"] == "applied" and not done["indexed"]
    granted[0] = False
    with pytest.raises(PermissionError):
        run(svc, "target", "bridge.plan", {"plan_id": done["plan_id"]})
    with pytest.raises(PermissionError):
        run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    assert callbacks[-1][1][0]["kind"] == "revocation"


def test_empty_project_does_not_inject_empty_context_framework(tmp_path, records):
    svc = service(tmp_path, records)
    result = run(svc, "target", "context.prepare", {"event": "question", "question": "continue"})
    assert result["context"] == "" and result["reminders"] == []


def retry_fixture(tmp_path, records, *, authorization=None, retry_override=None):
    state, imports, retries = ["running"], [], []
    def importer(ns, entry):
        imports.append(entry)
        return {"local_message_id": "imported-1", "receipt_id": "job-original", "indexed": False}
    def retry(ns, receipt, op):
        retries.append((ns, copy.deepcopy(receipt), op))
        if retry_override:
            return retry_override(receipt)
        state[0] = "pending"
        return dict(receipt)
    svc = service(tmp_path, records, importer=importer, retry_ingest=retry, authorize_records=authorization,
                  receipt_resolver=lambda ns, receipt: {**receipt, "indexed": state[0] == "succeeded", "engine_state": state[0]})
    p = preview(svc, source_package(svc))
    plan = run(svc, "target", "bridge.commit", {"preview_token": p["preview_token"]})
    state[0] = "failed"
    payload = {"plan_id": plan["plan_id"], "item_id": "m1", "confirmed": True}
    return svc, state, imports, retries, payload


def test_explicit_failed_retry_uses_same_job_once_then_real_indexing_receipt(tmp_path, records):
    svc, state, imports, retries, payload = retry_fixture(tmp_path, records)
    assert svc.snapshot("target")["bridge_plans"][0]["status"] == "failed"
    assert run(svc, "target", "bridge.plan", {"plan_id": payload["plan_id"]})["status"] == "failed"
    assert retries == [] and len(imports) == 1
    retried = svc.execute("target", "bridge.retry_failed", payload, "retry-once")
    assert retried["status"] == "accepted_pending" and not retried["indexed"]
    assert retried["records"][0]["receipt"]["receipt_id"] == "job-original"
    assert retried["records"][0]["retry_history"][0]["status"] == "accepted"
    svc.execute("target", "bridge.retry_failed", payload, "retry-once")
    assert len(retries) == 1 and len(imports) == 1
    state[0] = "succeeded"
    final = run(svc, "target", "bridge.plan", {"plan_id": payload["plan_id"]})
    assert final["indexed"] and final["status"] == "applied"
    svc.execute("target", "bridge.retry_failed", payload, "retry-once")
    assert len(retries) == 1


@pytest.mark.parametrize("state", ["pending", "running", "cancelled", "succeeded"])
def test_failed_retry_rejects_nonfailed_jobs_without_backend_call(tmp_path, records, state):
    svc, job_state, imports, retries, payload = retry_fixture(tmp_path, records)
    job_state[0] = state
    with pytest.raises(BridgeError, match="confirmed failed|unchanged accepted"):
        svc.execute("target", "bridge.retry_failed", payload, "not-failed")
    assert retries == [] and len(imports) == 1


def test_failed_retry_requires_confirmation_scoped_plan_and_unique_operation(tmp_path, records):
    svc, state, imports, retries, payload = retry_fixture(tmp_path, records)
    for confirmed in [False, "true", 1, None]:
        with pytest.raises(BridgeError, match="confirmed"):
            svc.execute("target", "bridge.retry_failed", {**payload, "confirmed": confirmed}, "not-confirmed")
    with pytest.raises(BridgeError, match="operation_id"):
        svc.execute("target", "bridge.retry_failed", payload, "")
    with pytest.raises(BridgeError, match="namespace"):
        svc.execute("other-project", "bridge.retry_failed", payload, "wrong-project")
    with pytest.raises(BridgeError, match="Unknown bridge"):
        svc.execute("target", "bridge.retry_failed", {**payload, "receipt_id": "replacement"}, "injection")
    assert not retries
    svc.execute("target", "bridge.retry_failed", payload, "retry-one")
    pkg = source_package(svc)
    another = svc.bridge.commit("target", svc.bridge.preview("target", pkg, ["m1"])["preview_token"])
    with pytest.raises(BridgeError, match="different record"):
        svc.execute("target", "bridge.retry_failed", {**payload, "plan_id": another["plan_id"]}, "retry-one")
    assert len(retries) == 1


def test_failed_retry_permission_checked_before_attempt_reservation(tmp_path, records):
    allowed = [True]
    def auth(ns, items):
        if not allowed[0]:
            raise PermissionError("authorization changed")
    svc, _, _, retries, payload = retry_fixture(tmp_path, records, authorization=auth)
    allowed[0] = False
    revision = svc.bridge.revision("target")
    with pytest.raises(PermissionError):
        svc.execute("target", "bridge.retry_failed", payload, "not-authorized")
    assert svc.bridge.revision("target") == revision
    assert svc.bridge.get_plan("target", payload["plan_id"])["records"][0]["retry_history"] == []
    assert not retries


def test_backend_recovery_rejection_preserved_and_same_operation_not_reexecuted(tmp_path, records):
    class AuditRejected(RuntimeError):
        status_code = 409
    def reject(receipt):
        raise AuditRejected("native ingest recovery audit rejected")
    svc, _, imports, retries, payload = retry_fixture(tmp_path, records, retry_override=reject)
    with pytest.raises(AuditRejected) as exc:
        svc.execute("target", "bridge.retry_failed", payload, "rejected-attempt")
    assert exc.value.status_code == 409
    repeated = svc.execute("target", "bridge.retry_failed", payload, "rejected-attempt")
    assert repeated["status"] == "failed" and not repeated["indexed"]
    assert repeated["retry_operation"]["status"] == "rejected"
    assert len(retries) == 1 and len(imports) == 1


def test_bounded_explicit_attempts_and_reserved_unknown_outcome_never_autoretry(tmp_path, records):
    svc, state, _, retries, payload = retry_fixture(tmp_path, records)
    for i in range(3):
        state[0] = "failed"
        svc.execute("target", "bridge.retry_failed", payload, f"explicit-{i}")
    state[0] = "failed"
    with pytest.raises(BridgeError, match="retry limit"):
        svc.execute("target", "bridge.retry_failed", payload, "fourth")
    assert len(retries) == 3
    other, _, _, other_retries, other_payload = retry_fixture(tmp_path / "other", records)
    plan = other.bridge.get_plan("target", other_payload["plan_id"])
    other.bridge.reserve_retry("target", plan["plan_id"], "m1", "interrupted-op", plan["records"][0]["receipt"], "failed")
    unknown = other.execute("target", "bridge.retry_failed", other_payload, "interrupted-op")
    assert any(e["error"] == "retry_outcome_unknown" for e in unknown["errors"])
    with pytest.raises(BridgeError, match="outcome is unknown"):
        other.execute("target", "bridge.retry_failed", other_payload, "different-op")
    assert other_retries == []


def test_retry_cannot_replace_source_job_or_claim_indexing_from_submission(tmp_path, records):
    svc, _, _, retries, payload = retry_fixture(tmp_path, records, retry_override=lambda receipt: {**receipt, "receipt_id": "new-job"})
    with pytest.raises(BridgeError, match="preserve the original"):
        svc.execute("target", "bridge.retry_failed", payload, "identity-change")
    plan = svc.bridge.get_plan("target", payload["plan_id"])
    assert not plan["indexed"] and plan["records"][0]["receipt"]["receipt_id"] == "job-original"
    assert plan["records"][0]["retry_history"][0]["status"] == "rejected"
    assert len(retries) == 1
