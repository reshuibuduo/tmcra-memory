from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request

import uvicorn

from conftest import FakeMemoryModel
from tmcra_local.api import create_app
from tmcra_local.http_transport import open_request


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "integrations/local-agent-hooks/hooks/run_hook.mjs"


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(base_url: str, token: str, path: str, method: str = "GET") -> dict:
    request = Request(
        base_url + path,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with open_request(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _run_hook(
    node: str,
    event: str,
    platform: str,
    payload: dict,
    environment: dict[str, str],
) -> tuple[dict, str]:
    completed = subprocess.run(
        [node, str(HOOK), event, platform],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=environment,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout), completed.stderr


def test_codex_and_claude_hooks_use_the_real_local_api_contract(
    config_path: Path, tmp_path: Path
) -> None:
    node = shutil.which("node")
    if not node:
        raise AssertionError("Node.js is required for the local integration contract test")
    app = create_app(config_path, verify_models=False)
    engine = app.state.engine
    engine.graph_factory.retrieval_mode = "heuristic"
    engine.graph_factory.node_model_path = ""
    engine.graph_factory.path_model_path = ""
    engine.graph_factory.graph_environment.update(
        {
            "TMCRA_WRITE_EMBEDDER_INDEX_MODE": "off",
            "TMCRA_EMBEDDER_INDEX_RECALL_MODE": "off",
            "TMCRA_EMBEDDER_PRE_RECALL_MODE": "off",
            "TMCRA_EMBEDDER_FUSION_MODE": "off",
        }
    )
    fake = FakeMemoryModel(engine._record_usage)
    engine.llm = fake
    engine.writer.flash_client = fake
    engine.writer.pro_client = fake

    port = _port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    token_path = Path(app.state.local_token_path)
    token = token_path.read_text(encoding="utf-8").strip()
    integration_path = tmp_path / "integration.json"
    integration_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "baseUrl": f"http://127.0.0.1:{port}",
                "tokenFile": str(token_path),
                "stateDir": str(tmp_path / "integration-state"),
                "topK": 16,
                "userVisibility": "both",
                "timeoutMs": 20_000,
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "TMCRA_LOCAL_INTEGRATION_CONFIG": str(integration_path),
    }
    workspace = tmp_path / "shared-project"
    workspace.mkdir()
    marker = "agent-hook-e2e-completed-parser"

    try:
        first, first_error = _run_hook(
            node,
            "user-prompt",
            "codex",
            {
                "session_id": "codex-thread-a",
                "turn_id": "codex-turn-a",
                "cwd": str(workspace),
                "prompt": "Start the parser task; api_key=local-fixture-secret",  # public-audit: allow-test-fixture
            },
            environment,
        )
        assert first == {"continue": True}
        _run_hook(
            node,
            "stop",
            "codex",
            {
                "session_id": "codex-thread-a",
                "turn_id": "codex-turn-a",
                "cwd": str(workspace),
                "last_assistant_message": f"{marker}: parser task is complete.",
            },
            environment,
        )
        second, second_error = _run_hook(
            node,
            "user-prompt",
            "claude-code",
            {
                "session_id": "claude-thread-b",
                "turn_id": "claude-turn-b",
                "cwd": str(workspace),
                "prompt": "Continue the parser work from the other tool.",
            },
            environment,
        )
        context = second["hookSpecificOutput"]["additionalContext"]
        assert marker in context
        assert 'trust="untrusted"' in context
        assert token not in first_error + second_error + json.dumps(first) + json.dumps(second)

        projects = _request(f"http://127.0.0.1:{port}", token, "/v1/projects")[
            "projects"
        ]
        assert len(projects) == 1
        project_id = projects[0]["project_id"]
        messages = _request(
            f"http://127.0.0.1:{port}",
            token,
            f"/v1/messages?project_id={quote(project_id)}&limit=20",
        )["messages"]
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "user",
        ]
        assert len({message["session_id"] for message in messages}) == 2
        codex_user = next(
            message
            for message in messages
            if message["role"] == "user" and message["source_app"] == "codex"
        )
        assert "local-fixture-secret" not in codex_user["content"]
        assert "[REDACTED]" in codex_user["content"]
        _request(
            f"http://127.0.0.1:{port}",
            token,
            f"/v1/projects/{quote(project_id)}",
            method="DELETE",
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
