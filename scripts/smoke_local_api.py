#!/usr/bin/env python3
"""Exercise the installed loopback API without printing local credentials.

The test creates a unique project, writes separate user and assistant source
messages, recalls both roles, builds the graph and personal knowledge, checks
the local usage ledger, verifies message deletion, and removes the project.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request

from tmcra_local.http_transport import open_request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = ROOT / ".tmcra/config/runtime/secrets/local-api.token"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class SmokeFailure(RuntimeError):
    """A safe, user-facing smoke-test failure."""


def _loopback_base_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SmokeFailure("base URL must be an HTTP(S) loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SmokeFailure("base URL must not contain credentials, query, or fragment")
    hostname = parsed.hostname.casefold()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise SmokeFailure("refusing to send the local bearer token off loopback")
        except ValueError as exc:
            raise SmokeFailure("refusing a non-loopback API host") from exc
    if parsed.path not in {"", "/"}:
        raise SmokeFailure("base URL must not contain a path")
    return value.rstrip("/")


def _token(path: Path) -> str:
    value = os.environ.get("TMCRA_LOCAL_TOKEN", "").strip()
    if not value:
        try:
            value = path.expanduser().resolve().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SmokeFailure(f"local token file is unavailable: {path}") from exc
    if len(value) < 24:
        raise SmokeFailure("local bearer token is missing or invalid")
    return value


class Client:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        self.base_url = _loopback_base_url(base_url)
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticate: bool = True,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if authenticate:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with open_request(request, timeout=self.timeout) as response:
                status = int(response.getcode())
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code in expected:
                return {"_status": exc.code}
            raise SmokeFailure(f"{method} {path} returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise SmokeFailure(f"{method} {path} could not reach the local API") from exc
        if status not in expected:
            raise SmokeFailure(f"{method} {path} returned unexpected HTTP {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SmokeFailure(f"{method} {path} returned an oversized response")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise SmokeFailure(f"{method} {path} did not return a JSON object")
        return result


def _contains(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return marker in value
    if isinstance(value, dict):
        return any(_contains(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, marker) for item in value)
    return False


def run(
    client: Client, *, allow_knowledge_fallback: bool = False
) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    project_id = f"local-release-smoke-{run_id}"
    session_id = f"session-{run_id}"
    user_marker = f"smoke-user-{run_id}"
    assistant_marker = f"smoke-assistant-{run_id}"
    user_message_id = ""
    created_project = False

    health = client.request("GET", "/v1/health", authenticate=False)
    if (
        health.get("status") != "ok"
        or health.get("mode") != "local-only"
        or health.get("bind_host") not in {"127.0.0.1", "::1", "localhost"}
        or health.get("contains_production_control_plane") is not False
    ):
        raise SmokeFailure("health response does not confirm a healthy local-only runtime")
    client.request(
        "GET", "/v1/projects", authenticate=False, expected=(401,)
    )

    try:
        common = {
            "project_id": project_id,
            "project_title": "Disposable local release smoke test",
            "session_id": session_id,
            "session_title": "Cross-tool continuity",
            "source_app": "tmcra-release-smoke",
            "native_thread_id": session_id,
        }
        user = client.request(
            "POST",
            "/v1/messages",
            {
                **common,
                "role": "user",
                "content": (
                    f"{user_marker}: the project codename is Orion and the retry "
                    "policy uses exponential backoff with a five-attempt cap."
                ),
                "native_message_id": f"user-{run_id}",
                "visibility": "both",
            },
        )
        created_project = True
        user_message_id = str(user.get("message_id") or "")
        if not user_message_id or len(user.get("scopes") or []) != 2:
            raise SmokeFailure("user message did not commit to global and project scopes")

        assistant = client.request(
            "POST",
            "/v1/messages",
            {
                **common,
                "role": "assistant",
                "content": (
                    f"{assistant_marker}: the agent completed the loopback API "
                    "integration and recorded the next implementation step."
                ),
                "native_message_id": f"assistant-{run_id}",
                "visibility": "project",
            },
        )
        if not assistant.get("message_id") or len(assistant.get("scopes") or []) != 1:
            raise SmokeFailure("assistant message did not commit to the project scope")

        recalled = client.request(
            "POST",
            "/v1/recall",
            {"project_id": project_id, "query": f"{user_marker} {assistant_marker}", "top_k": 32},
        )
        windows = recalled.get("evidence_windows") or []
        roles = {str(item.get("actor_role") or "") for item in windows}
        if not {"user", "assistant"}.issubset(roles):
            raise SmokeFailure("recall did not preserve separate user and assistant provenance")
        if len(recalled.get("resolved_scopes") or []) != 2:
            raise SmokeFailure("recall did not resolve global and current-project scopes")

        messages = client.request(
            "GET", f"/v1/messages?{urlencode({'project_id': project_id, 'limit': 20})}"
        ).get("messages") or []
        if len(messages) != 2:
            raise SmokeFailure("message inspection did not return both source records")

        graph = client.request(
            "GET", f"/v1/projects/{quote(project_id, safe='')}/graph"
        )
        if int((graph.get("counts") or {}).get("sessions") or 0) < 1 or not graph.get("nodes"):
            raise SmokeFailure("visual graph did not contain the smoke-test session and nodes")

        knowledge = client.request(
            "POST", f"/v1/projects/{quote(project_id, safe='')}/knowledge/build"
        )
        if knowledge.get("schema_version") != "tmcra.personal-knowledge.1":
            raise SmokeFailure("personal knowledge returned an unexpected schema")
        knowledge_state = str(knowledge.get("projection_state") or "")
        knowledge_pages = knowledge.get("pages") or []
        knowledge_evidence = knowledge.get("evidence_catalog") or []
        knowledge_generated_by = str(knowledge.get("generated_by") or "")
        if knowledge_state != "ready":
            if not allow_knowledge_fallback:
                raise SmokeFailure(
                    "personal knowledge did not complete with the configured generation model"
                )
        elif (
            knowledge_generated_by != "local-personal-knowledge-agent"
            or not knowledge_pages
            or not knowledge_evidence
        ):
            raise SmokeFailure(
                "personal knowledge did not return grounded model-generated pages"
            )

        usage = client.request(
            "GET", f"/v1/usage?{urlencode({'project_id': project_id, 'limit': 50})}"
        )
        if int((usage.get("totals") or {}).get("calls") or 0) < 1:
            raise SmokeFailure("local usage ledger did not record generation calls")

        deleted = client.request(
            "DELETE", f"/v1/messages/{quote(user_message_id, safe='')}"
        )
        if deleted.get("deleted") is not True:
            raise SmokeFailure("message deletion was not confirmed")
        remaining = client.request(
            "GET", f"/v1/messages?{urlencode({'project_id': project_id, 'limit': 20})}"
        ).get("messages") or []
        if any(str(item.get("message_id") or "") == user_message_id for item in remaining):
            raise SmokeFailure("deleted source message is still listed")
        after_delete = client.request(
            "POST",
            "/v1/recall",
            {"project_id": project_id, "query": user_marker, "top_k": 32},
        )
        if _contains(after_delete.get("evidence_windows") or [], user_marker):
            raise SmokeFailure("deleted source content is still recallable")

        return {
            "schema_version": "tmcra.local-api-smoke.1",
            "status": "passed",
            "local_only": True,
            "resolved_scope_count": len(recalled.get("resolved_scopes") or []),
            "recalled_roles": sorted(roles),
            "graph_node_count": len(graph.get("nodes") or []),
            "knowledge_schema": knowledge.get("schema_version"),
            "knowledge_projection_state": knowledge_state,
            "knowledge_generated_by": knowledge_generated_by,
            "knowledge_page_count": len(knowledge_pages),
            "knowledge_evidence_count": len(knowledge_evidence),
            "usage_calls": int((usage.get("totals") or {}).get("calls") or 0),
            "message_deletion_verified": True,
            "credential_printed": False,
        }
    finally:
        if created_project:
            try:
                client.request(
                    "DELETE", f"/v1/projects/{quote(project_id, safe='')}"
                )
            except SmokeFailure:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a running TMCRA local API")
    parser.add_argument("--base-url", default="http://127.0.0.1:2009")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--allow-knowledge-fallback",
        action="store_true",
        help=(
            "accept the deterministic fallback when Personal Knowledge is "
            "deliberately disabled; the default requires model-generated pages"
        ),
    )
    args = parser.parse_args(argv)
    try:
        report = run(
            Client(args.base_url, _token(args.token_file), args.timeout),
            allow_knowledge_fallback=args.allow_knowledge_fallback,
        )
    except SmokeFailure as exc:
        print(
            json.dumps(
                {
                    "schema_version": "tmcra.local-api-smoke.1",
                    "status": "failed",
                    "detail": str(exc),
                    "credential_printed": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
