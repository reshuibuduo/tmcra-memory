import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tmcra_local import api
from conftest import build_test_engine


@pytest.fixture
def client(config_path, monkeypatch):
    engine = build_test_engine(config_path)
    monkeypatch.setattr(api, "LocalMemoryEngine", lambda *a, **k: engine)
    app = api.create_app(config_path, verify_models=False)
    token = Path(app.state.local_token_path).read_text().strip()
    with TestClient(app) as value:
        value.headers["Authorization"] = "Bearer " + token
        value.engine = engine
        yield value


def call(client, ns, action, payload, op=None, status=200):
    response = client.post(f"/v1/projects/{ns}/workflows", json={"action": action, "payload": payload, "operation_id": op or uuid.uuid4().hex})
    assert response.status_code == status, response.text
    return response.json()


def seed(client, ns="one", ident="message-1", text="Use bounded retries with a clear stopping condition."):
    result = client.post("/v1/messages", json={"project_id": ns, "session_id": ns+"-session", "role": "user", "content": text, "message_id": ident})
    assert result.status_code == 200, result.text
    return ident


def test_four_workflows_auth_history_and_privacy(client):
    seed(client)
    assert client.get("/workflows/workflow-center.html").status_code == 200
    assert client.get("/workflows/unknown.js").status_code == 404
    token = client.headers.pop("Authorization")
    assert client.get("/v1/projects/one/workflows").status_code == 401
    client.headers["Authorization"] = token
    call(client,"one","task.upsert", {"id":"t", "title":"Fix retry", "version":"v1", "evidence_ids":["message-1"]})
    call(client,"one","task.receipt", {"id":"t", "outcome":"passed", "summary":"assistant says fixed", "version":"v1"})
    snap = client.get("/v1/projects/one/workflows").json()
    assert snap["tasks"][0]["verification_state"] == "reported"
    call(client,"two","task.upsert", {"id":"t", "title":"Cross source", "evidence_ids":["message-1"]}, status=404)
    d = call(client,"one","decision.create", {"id":"d", "text":"Keep retries bounded", "evidence_ids":["message-1"]})
    call(client,"one","decision.create", {"id":"e", "text":"Use 3 retries", "depends_on":["d"]})
    call(client,"one","decision.revise", {"id":"d", "text":"Use exponential delay", "reason":"failure evidence"})
    snap = client.get("/v1/projects/one/workflows").json()
    assert next(x for x in snap["decisions"] if x["id"] == "e")["needs_review"]
    historical = client.get("/v1/projects/one/workflows", params={"as_of":d["recorded_at"]}).json()
    assert len(historical["decisions"]) == 1
    assert historical["decisions"][0]["text"] == "Keep retries bounded"
    call(client,"one","attempt.record", {"id":"a", "task_id":"t", "outcome":"failed", "config":{"tries":3}, "summary":"timeout", "retry_conditions":["change delay"]})
    call(client,"one","reminder.upsert", {"id":"r", "title":"Check retry cap", "reason":"Previous timeout", "events":["question"], "query_terms":["retry"]})
    context = call(client,"one","context.prepare", {"event":"question", "question":"retry this"})
    assert len(context["reminders"]) == 1
    call(client,"one","reminder.act", {"id":"r", "disposition":"snooze", "snooze_seconds":60})
    assert not call(client,"one","context.prepare", {"event":"question", "question":"retry this"})["reminders"]
    assert client.delete("/v1/messages/message-1").status_code == 200
    historical = client.get("/v1/projects/one/workflows", params={"as_of":d["recorded_at"]}).json()
    assert historical["tasks"] == [] and historical["decisions"] == []


def test_selective_import_real_index_retry_relay_and_delete(client):
    seed(client)
    package = call(client,"one","bridge.export", {"selected_ids":["message-1"], "mode":"selected_sources"})
    preview = call(client,"two","bridge.preview", {"package":package, "selected_ids":["message-1"]})
    assert preview["can_commit"]
    assert client.engine.store.messages(project_id="two") == []
    plan = call(client,"two","bridge.commit", {"preview_token":preview["preview_token"]})
    assert plan["indexed"] is True, plan
    local_id = plan["records"][0]["local_id"]
    assert len(client.engine.store.messages(project_id="two")) == 1
    # Actual source graph, not merely bridge metadata.
    assert client.engine.writer_store.source_info(client.engine.project_scope("two"), local_id)["source_record_id"]
    again = call(client,"two","bridge.commit", {"preview_token":preview["preview_token"]})
    assert again["plan_id"] == plan["plan_id"] and len(client.engine.store.messages(project_id="two")) == 1
    relay = call(client,"two","bridge.export", {"selected_ids":[local_id], "mode":"selected_sources"})
    assert relay["items"][0]["source"] == package["items"][0]["source"]
    assert relay["items"][0]["item_sha256"] == package["items"][0]["item_sha256"]
    assert client.delete("/v1/messages/"+local_id).status_code == 200
    denied = call(client,"two","bridge.preview", {"package":package, "selected_ids":["message-1"]})
    assert denied["blocked"] and not denied["can_commit"]
    call(client,"two","bridge.commit", {"preview_token":denied["preview_token"]}, status=409)


def test_import_failure_is_retryable_and_conflict_does_not_overwrite(client, monkeypatch):
    seed(client)
    package = call(client,"one","bridge.export", {"selected_ids":["message-1"], "mode":"selected_sources"})
    preview = call(client,"two","bridge.preview", {"package":package, "selected_ids":["message-1"]})
    original = client.engine.ingest_message
    monkeypatch.setattr(client.engine,"ingest_message",lambda **k: (_ for _ in ()).throw(RuntimeError("simulated transport failure")))
    failed = call(client,"two","bridge.commit", {"preview_token":preview["preview_token"]})
    assert not failed["indexed"] and failed["errors"]
    monkeypatch.setattr(client.engine,"ingest_message", original)
    retry = call(client,"two","bridge.plan", {"plan_id":failed["plan_id"]})
    assert retry["indexed"]
    call(client,"two","bridge.plan", {"plan_id":failed["plan_id"]})
    assert len(client.engine.store.messages(project_id="two")) == 1


def test_secret_export_requires_explicit_field_approval(client):
    seed(client, text="password: example-only-secret")
    call(client,"one","bridge.export", {"selected_ids":["message-1"], "mode":"selected_sources"},status=409)
    package=call(client,"one","bridge.export", {"selected_ids":["message-1"], "mode":"selected_sources", "allow_sensitive_fields":["text"]})
    assert package["items"][0]["sensitive_fields"] == ["text"]


def test_same_package_into_two_projects_has_isolated_sessions(client):
    seed(client)
    package=call(client,"one","bridge.export", {"selected_ids":["message-1"],"mode":"selected_sources"})
    for project in ["two", "three"]:
        preview=call(client,project,"bridge.preview", {"package":package,"selected_ids":["message-1"]})
        result=call(client,project,"bridge.commit", {"preview_token":preview["preview_token"]})
        assert result["indexed"], result
        assert len(client.engine.store.messages(project_id=project)) == 1
