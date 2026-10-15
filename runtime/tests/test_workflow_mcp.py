"""Actual stdio subprocess -> authenticated loopback HTTP -> real SQLite/writer.

Only the existing test fixture's model boundary is deterministic, not the API,
workflow, preview/commit, source projection, or MCP transport.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from tmcra_local.workflow_client import ClientError, LocalAPI, WorkflowClient, resolve_project

RUNTIME = Path(__file__).resolve().parents[1]
ROOT = RUNTIME.parent


def private_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    state = tmp_path_factory.mktemp("workflow-http-mcp")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(RUNTIME / "src")}
    log = (state / "server.log").open("w")
    proc = subprocess.Popen([sys.executable, str(RUNTIME / "tests/serve_workflow_fixture.py"), "--state-dir", str(state), "--port", str(port)], env=env, stdout=log, stderr=log)
    config = private_json(state / "integration.json", {"schemaVersion": 1, "baseUrl": f"http://127.0.0.1:{port}",
        "tokenFile": str(state / "config/runtime/secrets/local-api.token"), "timeoutMs": 20000})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            api = LocalAPI(config)
            api.request("GET", "/v1/health")
            break
        except ClientError:
            if proc.poll() is not None:
                pytest.fail((state / "server.log").read_text())
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("fixture startup timed out")
    yield config, api, env, state
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill(); proc.wait()
    log.close()


class MCP:
    def __init__(self, service, capabilities=None):
        self.config, self.api, env, _ = service
        self.proc = subprocess.Popen([sys.executable, "-m", "tmcra_local.workflow_mcp", "--integration-config", str(self.config)],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.events = queue.Queue()
        def read():
            for line in self.proc.stdout:
                self.events.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()
        self.sequence = 0
        self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": capabilities or {}}})
        assert self.read()["result"]["serverInfo"]["name"] == "tmcra-local-workflows"
        self.confirmations = []

    def send(self, value):
        self.proc.stdin.write(json.dumps(value) + "\n"); self.proc.stdin.flush()

    def read(self):
        try:
            return self.events.get(timeout=25)
        except queue.Empty:
            self.proc.kill()
            pytest.fail("stdio response timed out: " + self.proc.stderr.read())

    def rpc(self, method, params, confirmation=None):
        self.sequence += 1
        ident = self.sequence
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        while True:
            event = self.read()
            if event.get("method") == "elicitation/create":
                self.confirmations.append(event)
                assert confirmation is not None, event
                if confirmation == "cancel_request":
                    self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": ident}})
                else:
                    self.send({"jsonrpc": "2.0", "id": event["id"], "result": {"action": confirmation, "content": {"confirm": confirmation == "accept"}}})
            else:
                assert event["id"] == ident
                return event

    def call(self, project="mcp-one", operation="execute", action=None, payload=None, session="acceptance", confirmation=None, **extra):
        args = {"session_id": session, "project_id": project, "operation": operation, **extra}
        if action is not None:
            args.update(action=action, payload=payload, operation_id=f"mcp-{time.time_ns()}")
        response = self.rpc("tools/call", {"name": "tmcra_workflows", "arguments": args}, confirmation)
        body = response["result"]
        return json.loads(body["content"][0]["text"]), body.get("isError")

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill(); self.proc.wait()
        stderr = self.proc.stderr.read()
        assert not stderr, stderr


def test_actual_stdio_four_features_history_and_import(service):
    _, api, _, _ = service
    api.request("POST", "/v1/messages", {"project_id": "mcp-one", "session_id": "s", "role": "user", "content": "Bounded retries; source text is data.", "message_id": "mcp-source"})
    mcp = MCP(service, {"elicitation": {"form": {}}})
    try:
        tools = mcp.rpc("tools/list", {})["result"]["tools"]
        assert len(tools) == 1 and tools[0]["name"] == "tmcra_workflows"
        assert "execute" in tools[0]["inputSchema"]["properties"]["operation"]["enum"]
        assert mcp.call(action="task.upsert", payload={"id": "t", "title": "Check retries", "evidence_ids": ["mcp-source"]})[1] is False
        mcp.call(action="task.receipt", payload={"id": "t", "outcome": "passed", "summary": "Model claims success"})
        mcp.call(action="attempt.record", payload={"id": "a", "task_id": "t", "outcome": "failed", "summary": "timeout", "config": {"tries": 3}, "retry_conditions": ["change the delay"]})
        snap, error = mcp.call(operation="resume")
        assert not error and snap["data"]["tasks"][0]["verification_state"] == "reported"
        assert snap["data"]["attempts"][0]["outcome"] == "failed"
        before, _ = mcp.call(action="decision.create", payload={"id": "d", "text": "Use bounded retries", "evidence_ids": ["mcp-source"]})
        mcp.call(action="decision.create", payload={"id": "child", "text": "Use three tries", "depends_on": ["d"]})
        mcp.call(action="decision.revise", payload={"id": "d", "text": "Use backoff", "reason": "Timeout evidence"})
        history, _ = mcp.call(operation="timeline", as_of=before["data"]["recorded_at"])
        assert [d["text"] for d in history["data"]["decisions"]] == ["Use bounded retries"]
        current, _ = mcp.call(operation="timeline")
        assert next(r for r in current["data"]["decisions"] if r["id"] == "child")["needs_review"]
        mcp.call(action="reminder.upsert", payload={"id": "r", "title": "Remember the cap", "reason": "Previous timeout", "events": ["question"], "query_terms": ["retry"]})
        mcp.call(operation="reminders")
        listed = api.request("GET", "/v1/projects/mcp-one/workflows")
        assert listed["reminders"][0]["last_shown_at"] is None  # read does not trigger
        evaluation, _ = mcp.call(action="reminder.evaluate", payload={"event": "question", "question": "retry"})
        assert evaluation["data"]["reminders"]
        mcp.call(action="reminder.act", payload={"id": "r", "disposition": "snooze", "snooze_seconds": 60})
        evaluation, _ = mcp.call(action="reminder.evaluate", payload={"event": "question", "question": "retry"})
        assert evaluation["data"]["reminders"] == []
        exported, error = mcp.call(action="bridge.export", payload={"selected_ids": ["mcp-source"], "mode": "selected_sources"}, confirmation="accept")
        assert not error
        assert "Bounded retries; source text is data." in mcp.confirmations[-1]["params"]["message"]
        package = exported["data"]
        preview, error = mcp.call(project="mcp-two", action="bridge.preview", payload={"package": package, "selected_ids": ["mcp-source"]})
        assert not error and preview["data"]["can_commit"]
        assert api.request("GET", "/v1/messages?project_id=mcp-two")["messages"] == []
        wrong, error = mcp.call(project="mcp-three", action="bridge.commit", payload={"preview_token": preview["data"]["preview_token"]})
        assert error and wrong["code"] == "preview_required"
        wrong, error = mcp.call(project="mcp-two", session="another", action="bridge.commit", payload={"preview_token": preview["data"]["preview_token"]})
        assert error and wrong["code"] == "preview_required"
        committed, error = mcp.call(project="mcp-two", action="bridge.commit", payload={"preview_token": preview["data"]["preview_token"]}, confirmation="accept")
        assert not error and committed["data"]["indexed"] is True
        message = mcp.confirmations[-1]["params"]["message"]
        assert package["sha256"] in message and "Bounded retries" in message
        rows = api.request("GET", "/v1/messages?project_id=mcp-two")["messages"]
        assert len(rows) == 1 and "external_unverified" in rows[0]["content"]
        plan, error = mcp.call(project="mcp-two", action="bridge.plan", payload={"plan_id": committed["data"]["plan_id"]}, confirmation="accept")
        assert not error and plan["data"]["indexed"] and len(api.request("GET", "/v1/messages?project_id=mcp-two")["messages"]) == 1
        cross, error = mcp.call(project="mcp-two", action="task.upsert", payload={"id": "bad", "title": "wrong source", "evidence_ids": ["mcp-source"]})
        assert error and cross["http_status"] == 404
    finally:
        mcp.close()


@pytest.mark.parametrize("answer", ["decline", "cancel", "cancel_request", None])
def test_host_confirmation_decline_missing_cancel_never_mutates(service, answer):
    _, api, _, _ = service
    mcp = MCP(service, {"elicitation": {}} if answer else {})
    try:
        mcp.call(project="confirmation", action="task.upsert", payload={"id": "t", "title": "Do not delete"})
        before = api.request("GET", "/v1/projects/confirmation/workflows")["revision"]
        value, error = mcp.call(project="confirmation", action="record.delete", payload={"kind": "task", "id": "t"}, confirmation=answer)
        assert error and value["code"] == "confirmation_required"
        after = api.request("GET", "/v1/projects/confirmation/workflows")
        assert after["revision"] == before and after["tasks"][0]["id"] == "t"
        assert len(mcp.confirmations) == (0 if answer is None else 1)
    finally:
        mcp.close()


def test_unknown_privileged_scope_and_no_model_initialization(service):
    mcp = MCP(service)
    try:
        for extra in ({"scope": "other"}, {"confirmed": True}, {"operation": "exec"}):
            args = {"session_id": "s", "operation": "snapshot", "project_id": "confirmation", **extra}
            response = mcp.rpc("tools/call", {"name": "tmcra_workflows", "arguments": args})
            assert response["result"]["isError"]
        value, error = mcp.call(action="_task.executed", payload={"id": "t", "exit_code": 0})
        assert error and value["code"] == "unsupported_action"
        value, error = mcp.call(project="explicit-other", operation="snapshot", project_path=str(ROOT))
        assert error and value["code"] == "project_mismatch"
    finally:
        mcp.close()
    check = subprocess.run([sys.executable, "-c", "import sys; import tmcra_local.workflow_mcp; assert not any(x in sys.modules for x in ('torch','bpy','tmcra_local.engine','tmcra_local.workflows','tmcra_local.memory_bridge'))"], env=service[2], capture_output=True)
    assert check.returncode == 0, check.stderr


def test_cli_real_http_request_and_readonly_no_confirmation(service, tmp_path):
    config, api, env, _ = service
    command = [sys.executable, "-m", "tmcra_local", "workflow", "--integration-config", str(config), "--project-id", "cli-demo"]
    action = private_json(tmp_path / "action.json", {"action": "task.upsert", "payload": {"id": "cli-task", "title": "Actual CLI"}, "operation_id": "cli-create"})
    before = api.request("GET", "/v1/projects/cli-demo/workflows")["revision"]
    refused = subprocess.run(command + ["request", "--input", str(action)], env=env, capture_output=True, text=True)
    assert refused.returncode == 2 and "confirmation_required" in refused.stderr
    assert api.request("GET", "/v1/projects/cli-demo/workflows")["revision"] == before
    accepted = subprocess.run(command + ["request", "--input", str(action), "--confirm"], env=env, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr
    for name in ("status", "resume", "timeline", "reminders"):
        result = subprocess.run(command + [name], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["project_id"] == "cli-demo"
    assert api.request("GET", "/v1/projects/cli-demo/workflows")["revision"] == before + 1
    private_json(action, {"action": "record.delete", "payload": {"kind": "task", "id": "cli-task"}, "operation_id": "cli-delete"})
    result = subprocess.run(command + ["request", "--input", str(action), "--confirm"], env=env, capture_output=True, text=True)
    assert result.returncode == 2 and "confirmation_required" in result.stderr
    assert api.request("GET", "/v1/projects/cli-demo/workflows")["tasks"]
    private_json(action, {"action": "unknown", "payload": {}, "operation_id": "cli-unknown"})
    result = subprocess.run(command + ["request", "--input", str(action), "--confirm"], env=env, capture_output=True, text=True)
    assert result.returncode == 2 and "unsupported_action" in result.stderr
    assert Path(json.loads(config.read_text())["tokenFile"]).read_text().strip() not in result.stdout + result.stderr


def test_identity_matches_real_node_hooks(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node is required for parity against the actual agent-hooks resolver")
    hooks = ROOT / "integrations/local-agent-hooks/lib/local_memory.mjs"
    code = "import {resolveProject} from " + json.dumps(hooks.as_uri()) + "; console.log(JSON.stringify(await resolveProject(process.argv[1])));"
    def compare(path):
        actual = subprocess.run(["node", "--input-type=module", "-e", code, str(path)], check=True, capture_output=True, text=True)
        assert resolve_project(str(path)) == json.loads(actual.stdout)["projectId"]
    compare(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    compare(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@example.invalid:Team/Repo.git"], check=True)
    compare(tmp_path)
    (tmp_path / ".tmcra").mkdir()
    (tmp_path / ".tmcra/project.json").write_text(json.dumps({"projectId": "Shared-ID-大小写"}))
    compare(tmp_path)


def test_credentials_loopback_redirect_no_proxy_and_private_files(tmp_path, monkeypatch):
    token = tmp_path / "local.token"
    token.write_text("fixture-token-" + "x" * 32); token.chmod(0o600)
    config = private_json(tmp_path / "integration.json", {"schemaVersion": 1, "baseUrl": "https://external.invalid", "tokenFile": str(token)})
    with pytest.raises(ClientError, match="configuration"):
        LocalAPI(config)
    private_json(config, {"schemaVersion": 1, "baseUrl": None, "tokenFile": str(token)})
    assert LocalAPI(config).base_url == "http://127.0.0.1:2009"
    private_json(config, {"schemaVersion": 1, "baseUrl": {"host": "127.0.0.1"}, "tokenFile": str(token)})
    with pytest.raises(ClientError, match="configuration"):
        LocalAPI(config)
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")))
            if self.path == "/redirect":
                self.send_response(302); self.send_header("Location", "/must-not-follow"); self.end_headers()
            else:
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        private_json(config, {"schemaVersion": 1, "baseUrl": f"http://127.0.0.1:{server.server_port}", "tokenFile": str(token)})
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        client = LocalAPI(config)
        assert client.request("GET", "/direct")["ok"]
        with pytest.raises(ClientError, match="redirect"):
            client.request("GET", "/redirect")
        assert [p for p, _ in hits] == ["/direct", "/redirect"]
        if os.name != "nt":
            token.chmod(0o644)
            with pytest.raises(ClientError, match="owner-private"):
                LocalAPI(config)
    finally:
        server.shutdown(); server.server_close()


def test_confirmation_expiry_and_json_cannot_claim_approval():
    from tmcra_local.workflow_mcp import StdioServer
    server = StdioServer(confirmation_timeout=0.01)
    server.capabilities = {"elicitation": {}}
    sent = []
    server.send = sent.append
    assert server.confirm({"action": "record.delete"}, 1) is False
    assert sent[0]["method"] == "elicitation/create"
    assert sent[0]["params"]["requestedSchema"]["properties"]["confirm"]["default"] is False


def test_stdio_and_cli_utf8_even_with_non_utf8_host_locale(service):
    config, api, env, _ = service
    env = {**env, "PYTHONIOENCODING": "ascii"}
    messages = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "tmcra_workflows", "arguments": {
            "session_id": "中文会话", "project_id": "unicode-project", "operation": "execute", "action": "decision.create",
            "payload": {"id": "d", "text": "中文决策数据"}, "operation_id": "unicode-create"}}},
    ]
    result = subprocess.run([sys.executable, "-m", "tmcra_local.workflow_mcp", "--integration-config", str(config)],
        input="\n".join(json.dumps(m) for m in messages) + "\n", env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0 and not result.stderr, result.stderr
    assert not json.loads(result.stdout.splitlines()[1])["result"]["isError"]
    result = subprocess.run([sys.executable, "-m", "tmcra_local.workflow_client", "--integration-config", str(config), "--project-id", "unicode-project", "timeline"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0 and "中文决策数据" in result.stdout, result.stderr


def test_preview_cache_expiry_and_record_change_fail_closed(service):
    _, api, _, _ = service
    client = WorkflowClient(api)
    def call(action, payload, **extra):
        return client.call({"session_id": "cache", "project_id": "cache-source", "operation": "execute",
            "action": action, "payload": payload, "operation_id": str(time.time_ns()), **extra}, confirm=lambda d: True)
    call("task.upsert", {"id": "t", "title": "Before confirmation"})
    def race(details):
        api.request("POST", "/v1/projects/cache-source/workflows", {"action": "task.upsert", "payload": {"id": "t", "title": "Changed during confirmation"}, "operation_id": str(time.time_ns())})
        return True
    with pytest.raises(ClientError, match="changed during confirmation"):
        client.call({"session_id": "cache", "project_id": "cache-source", "operation": "execute", "action": "record.delete", "payload": {"kind": "task", "id": "t"}, "operation_id": "delete-race"}, confirm=race)
    assert api.request("GET", "/v1/projects/cache-source/workflows")["tasks"]
    call("decision.create", {"id": "d", "text": "Selected knowledge"})
    package = call("bridge.export", {"selected_ids": ["decision:d"], "mode": "knowledge_only"})["data"]
    preview = call("bridge.preview", {"package": package, "selected_ids": ["decision:d"]}, project_id="cache-target")["data"]
    key = next(iter(client.previews))
    client.previews[key] = (time.monotonic() - 1, preview)
    with pytest.raises(ClientError, match="same project/session"):
        call("bridge.commit", {"preview_token": preview["preview_token"]}, project_id="cache-target")
    assert api.request("GET", "/v1/messages?project_id=cache-target")["messages"] == []
