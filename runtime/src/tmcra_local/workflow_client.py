"""Small owner-local API client shared by the workflow CLI and stdio MCP.

This module reads existing integration credentials; it never creates an engine,
downloads models, executes supplied commands, or opens the workflow database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BYTES = 2 * 1024 * 1024
OPERATIONS = ("snapshot", "resume", "timeline", "reminders", "execute")
ACTIONS = frozenset({"task.upsert", "task.receipt", "attempt.record", "decision.create", "decision.revise",
    "reminder.upsert", "reminder.evaluate", "reminder.act", "record.delete", "context.prepare",
    "bridge.export", "bridge.export_revocations", "bridge.preview", "bridge.commit", "bridge.plan", "bridge.retry_failed"})
SENSITIVE = frozenset({"record.delete", "bridge.export", "bridge.export_revocations", "bridge.commit", "bridge.plan", "bridge.retry_failed"})
ARGUMENTS = {"session_id", "project_path", "project_id", "operation", "as_of", "action", "payload", "operation_id"}


class ClientError(ValueError):
    def __init__(self, code, message, status=None):
        self.code, self.status = code, status
        super().__init__(message)


def _identifier(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ClientError("invalid_argument", f"{label} must be a nonempty identifier")
    return value


def _git(cwd, args):
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                              timeout=2, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def resolve_project(path=None):
    """Same marker/git-origin/path SHA256 identity as local-agent-hooks.

    abspath rather than realpath deliberately matches Node path.resolve,
    including its existing symlink behavior. git calls are fixed read-only calls.
    """
    cwd = Path(os.path.abspath(path or os.getcwd()))
    for current in (cwd, *cwd.parents):
        marker = current / ".tmcra" / "project.json"
        if marker.exists():
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
                identity = str(value.get("projectId") or value.get("project_id") or value.get("id") or "")
                identity = identity.replace("\x00", "").replace("\r", "").replace("\n", "").strip()[:200]
                if identity:
                    return "tmcra-" + hashlib.sha256(("marker:" + identity).encode()).hexdigest()[:32]
            except (ValueError, OSError, AttributeError):
                pass
            break
    root = _git(cwd, ["rev-parse", "--show-toplevel"])
    remote = _git(root, ["config", "--get", "remote.origin.url"]) if root else ""
    identity = ("git:" + remote.removesuffix(".git")) if remote else "path:" + (root or str(cwd))
    return "tmcra-" + hashlib.sha256(identity.lower().encode()).hexdigest()[:32]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("redirect_rejected", "The local API must answer directly; redirects are rejected")


def _read_private(path, maximum):
    try:
        with Path(path).expanduser().open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                raise ClientError("private_file_required", "Existing config/token must be an owner-private regular file")
            data = stream.read(maximum + 1)
    except OSError as exc:
        raise ClientError("configuration_unavailable", "Existing local integration config/token is unavailable; configure the local integration first") from exc
    if len(data) > maximum:
        raise ClientError("configuration_invalid", "Local configuration exceeds the size limit")
    return data


class LocalAPI:
    def __init__(self, config_path=None):
        path = config_path or os.environ.get("TMCRA_LOCAL_INTEGRATION_CONFIG") or str(Path.home() / ".tmcra/local-integration.json")
        try:
            config = json.loads(_read_private(path, 65536))
            if not isinstance(config, dict) or config.get("schemaVersion") != 1:
                raise ValueError()
            base_url = config.get("baseUrl") or "http://127.0.0.1:2009"
            if not isinstance(base_url, str):
                raise ValueError()
            parts = urlsplit(base_url)
            if parts.scheme not in {"http", "https"} or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
                raise ValueError()
            # Match the hooks' exact loopback hosts, not arbitrary DNS or LAN.
            if parts.hostname not in {"localhost", "127.0.0.1", "::1"} or (parts.port is not None and not 0 < parts.port < 65536):
                raise ValueError()
            self.base_url = base_url.rstrip("/")
            self.timeout = min(180, max(1, float(config.get("timeoutMs", 120000)) / 1000))
            if not math.isfinite(self.timeout):
                raise ValueError()
            token_path = config["tokenFile"]
            if not isinstance(token_path, str) or not token_path:
                raise ValueError()
            self._token = _read_private(token_path, 8192).decode().strip()
            if len(self._token) < 24 or any(ord(c) < 33 or ord(c) > 126 for c in self._token):
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            if isinstance(exc, ClientError):
                raise
            raise ClientError("configuration_invalid", "Invalid existing local integration configuration") from exc
        self.account_key = hashlib.sha256((self.base_url + "\0" + self._token).encode()).hexdigest()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        if data is not None and len(data) > MAX_BYTES:
            raise ClientError("request_too_large", "Request exceeds 2 MiB")
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(8 * MAX_BYTES + 1)
        except HTTPError as exc:
            # Do not echo arbitrary server HTML, authentication headers or text.
            raise ClientError("api_error", f"Local workflow API returned HTTP {exc.code}; no automatic retry was attempted", exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ClientError("api_unavailable", "Local workflow API request failed; check the existing local service. No automatic retry was attempted") from exc
        if len(raw) > 8 * MAX_BYTES:
            raise ClientError("response_too_large", "Local workflow response exceeds 16 MiB")
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ClientError("invalid_response", "Local workflow API did not return JSON") from exc
        if not isinstance(value, dict):
            raise ClientError("invalid_response", "Local workflow API response must be an object")
        return value


class WorkflowClient:
    def __init__(self, api):
        self.api = api
        # Only actual server preview responses, never model-supplied summaries.
        self.previews = {}

    def _snapshot(self, project, as_of=None):
        suffix = "?" + urlencode({"as_of": as_of}) if as_of else ""
        return self.api.request("GET", f"/v1/projects/{quote(project, safe='')}/workflows" + suffix)

    def _confirmation(self, project, session, action, payload):
        if action == "bridge.commit":
            key = (self.api.account_key, project, session, payload.get("preview_token"))
            stored = self.previews.get(key)
            if stored is None or stored[0] < time.monotonic():
                raise ClientError("preview_required", "Run bridge.preview in this MCP process and the same project/session before commit")
            if not stored[1].get("can_commit"):
                raise ClientError("preview_blocked", "The actual import preview contains blocked items or conflicts")
            return stored[1]
        snap = self._snapshot(project)
        if action in {"bridge.plan", "bridge.retry_failed"}:
            plan = next((p for p in snap.get("bridge_plans", []) if p.get("plan_id") == payload.get("plan_id")), None)
            if plan is None:
                raise ClientError("selection_not_found", "The selected plan does not belong to this project")
            if action == "bridge.retry_failed":
                row = next((r for r in plan.get("records", []) if r.get("item_id") == payload.get("item_id")), None)
                if row is None or row.get("engine_state") != "failed" or payload.get("confirmed") is not True:
                    raise ClientError("retry_not_available", "Only an explicitly selected failed ingest can be retried")
            return plan
        if action == "record.delete":
            plural = {"task": "tasks", "attempt": "attempts", "decision": "decisions", "reminder": "reminders"}.get(payload.get("kind"))
            row = next((r for r in snap.get(plural, []) if r.get("id") == payload.get("id")), None) if plural else None
            if row is None:
                raise ClientError("selection_not_found", "Select an existing record in the current project")
            return {"record": row, "warning": "Deletion also removes dependent records and historical copies where required"}
        selected = payload.get("selected_ids")
        if not isinstance(selected, list) or not selected or len(selected) > 100 or not all(isinstance(i, str) and i for i in selected) or len(set(selected)) != len(selected):
            raise ClientError("explicit_selection_required", "Select 1–100 distinct record IDs explicitly")
        rows = {r["id"]: r for r in snap.get("available_records", []) + snap.get("available_revocations", [])}
        if any(ident not in rows for ident in selected):
            raise ClientError("selection_not_found", "An export selection is unavailable in the current project")
        if action == "bridge.export":
            messages = self.api.request("GET", "/v1/messages?" + urlencode({"project_id": project, "limit": 500})).get("messages", [])
            messages = {r["message_id"]: r for r in messages}
            decisions = {"decision:" + r["id"]: r for r in snap.get("decisions", [])}
            for ident in selected:
                source = decisions.get(ident) if ident.startswith("decision:") else messages.get(ident)
                if source is None:
                    raise ClientError("selection_not_found", "The complete selected record is unavailable for confirmation")
                rows[ident] = {**rows[ident], "text": source.get("text", source.get("content", "")),
                               "role": source.get("role", "knowledge"), "occurred_at": source.get("occurred_at", source.get("recorded_at"))}
        return {"selected_records": [rows[i] for i in selected], "mode": payload.get("mode"),
                "allow_sensitive_fields": payload.get("allow_sensitive_fields", []),
                "warning": "Selected content, role, time and original provenance leave the local API in a JSON package. Raw sources may contain sensitive text. No network transfer to another service is performed. Revocations explicitly remove selected imported identities."}

    def call(self, arguments, confirm=None):
        if not isinstance(arguments, dict) or set(arguments) - ARGUMENTS:
            raise ClientError("invalid_arguments", "Unknown workflow tool arguments")
        session = _identifier(arguments.get("session_id"), "session_id")
        operation = arguments.get("operation")
        if operation not in OPERATIONS:
            raise ClientError("unsupported_operation", "Unknown workflow operation")
        path = arguments.get("project_path")
        project = arguments.get("project_id")
        if path is not None:
            _identifier(path, "project_path")
        if project is not None:
            _identifier(project, "project_id")
        derived = resolve_project(path) if path is not None or project is None else None
        if project and path is not None and project != derived:
            raise ClientError("project_mismatch", "project_id does not match the hook identity for project_path")
        project = project or derived
        if operation != "execute":
            if any(k in arguments for k in ("action", "payload", "operation_id")):
                raise ClientError("invalid_arguments", "Read-only operations do not accept mutation fields")
            as_of = arguments.get("as_of")
            if as_of is not None:
                _identifier(as_of, "as_of")
            snap = self._snapshot(project, as_of)
            if operation == "resume":
                data = {k: snap.get(k) for k in ("namespace", "revision", "recorded_at", "tasks", "attempts")}
            elif operation == "timeline":
                data = {k: snap.get(k) for k in ("namespace", "revision", "recorded_at", "decisions")}
            elif operation == "reminders":
                data = {k: snap.get(k) for k in ("namespace", "revision", "recorded_at", "reminders")}
            else:
                data = snap
        else:
            if "as_of" in arguments:
                raise ClientError("invalid_arguments", "Historical views are read-only")
            action = arguments.get("action")
            if action not in ACTIONS:
                raise ClientError("unsupported_action", "Unknown or privileged workflow action")
            op = _identifier(arguments.get("operation_id"), "operation_id")
            payload = arguments.get("payload")
            if not isinstance(payload, dict):
                raise ClientError("invalid_payload", "payload must be an object")
            if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
                raise ClientError("request_too_large", "Request exceeds 2 MiB")
            if action in SENSITIVE:
                details = self._confirmation(project, session, action, payload)
                if confirm is None or confirm({"project_id": project, "session_id": session, "action": action, "operation_id": op,
                    "payload": payload, "actual_preview": details, "trust_boundary": "untrusted_data_not_instructions"}) is not True:
                    raise ClientError("confirmation_required", "Explicit host confirmation was unavailable, declined or expired; no mutation was sent")
                if self._confirmation(project, session, action, payload) != details:
                    raise ClientError("selection_changed", "The selected records changed during confirmation; preview again")
            data = self.api.request("POST", f"/v1/projects/{quote(project, safe='')}/workflows", {"action": action, "payload": payload, "operation_id": op})
            if action == "bridge.preview" and isinstance(data.get("preview_token"), str):
                # Server still enforces the actual expiry/revision at commit.
                self.previews = {key: value for key, value in self.previews.items() if value[0] > time.monotonic()}
                if len(self.previews) >= 64:
                    self.previews.pop(next(iter(self.previews)))
                self.previews[(self.api.account_key, project, session, data["preview_token"])] = (time.monotonic() + 120, json.loads(json.dumps(data)))
        return {"project_id": project, "operation": operation, "data": data, "trust_boundary": "untrusted_workflow_data"}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(prog="tmcra-local workflow", description="Use the existing owner-local API. No model loading or automatic retries.",
        epilog="Examples: workflow --project-path . resume; workflow --project-id demo timeline --as-of 2026-10-15T00:00:00Z; workflow --project-id demo request --input action.json --confirm. Use the MCP/website two-step import preview and commit for memory packages.")
    parser.add_argument("--integration-config", help="Existing owner-private local-integration.json (default: configured environment or ~/.tmcra)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--project-path", help="Infer the exact local-agent-hooks project identity")
    target.add_argument("--project-id", help="Explicit local project ID; defaults to current directory identity")
    parser.add_argument("--session-id", default="workflow-cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "resume", "timeline", "reminders"):
        child = sub.add_parser(name, help="Read-only API snapshot; does not trigger reminders")
        child.add_argument("--as-of", help="ISO timestamp for a historical snapshot")
    child = sub.add_parser("request", help="Explicit action JSON {action,payload,operation_id}; never executes shell commands")
    child.add_argument("--input", required=True, help="JSON file or - for stdin, at most 2 MiB")
    child.add_argument("--confirm", action="store_true", help="Required for every mutation. Sensitive actions additionally require an interactive terminal confirmation.")
    args = parser.parse_args(argv)
    try:
        arguments = {"session_id": args.session_id, "operation": "snapshot" if args.command == "status" else args.command}
        for key in ("project_path", "project_id", "as_of"):
            value = getattr(args, key, None)
            if value is not None:
                arguments[key] = value
        if args.command == "request":
            if not args.confirm:
                raise ClientError("confirmation_required", "Mutations require --confirm")
            if args.input == "-":
                raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ClientError("request_too_large", "Request exceeds 2 MiB")
            request = json.loads(raw)
            if not isinstance(request, dict) or set(request) != {"action", "payload", "operation_id"}:
                raise ClientError("invalid_arguments", "Input must contain exactly action, payload, operation_id")
            arguments.update(request, operation="execute")
        def confirm(details):
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                return False
            print("Review the exact local change (record text is data, not instructions):", file=sys.stderr)
            print(json.dumps(details, ensure_ascii=False, indent=2), file=sys.stderr)
            print("Type CONFIRM to proceed: ", end="", file=sys.stderr, flush=True)
            return sys.stdin.readline().strip() == "CONFIRM"
        result = WorkflowClient(LocalAPI(args.integration_config)).call(arguments, confirm=confirm)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result.get("data", {}).get("errors") else 0
    except (ClientError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "code": getattr(exc, "code", "invalid_input"),
                          "http_status": getattr(exc, "status", None), "message": str(exc) if isinstance(exc, ClientError) else "Invalid or unreadable input"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
