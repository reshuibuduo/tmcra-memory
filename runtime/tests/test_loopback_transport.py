"""Exercise actual HTTP boundaries using only disposable loopback listeners."""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from tmcra_local.llm import OpenAICompatibleClient
from tmcra_local.http_transport import is_loopback_url, open_request
from tmcra_local.generation_runtime import _generation_health, probe_generation_engine

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/smoke_local_api.py"
SPEC = importlib.util.spec_from_file_location("tmcra_network_smoke", SCRIPT)
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


@contextmanager
def server(*, status=200, location=None, payload=None):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            length = int(self.headers.get("Content-Length", "0"))
            seen.append((self.command, self.path, self.rfile.read(length)))
            self.send_response(status)
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload or {"ok": True}).encode())

        do_POST = do_GET

        def log_message(self, *_):
            pass

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}", seen
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_smoke_loopback_get_and_post_bypass_system_proxy():
    with server(payload={"proxy": True}) as (proxy, proxy_seen), server() as (url, seen):
        with patch.object(urllib.request, "_opener", None), \
             patch("urllib.request.getproxies", return_value={"http": proxy}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            client = SMOKE.Client(url, "synthetic-test-token", 2)
            assert client.request("GET", "/probe") == {"ok": True}
            assert client.request("POST", "/probe", {"content": "synthetic evidence"}) == {"ok": True}
        assert proxy_seen == []
        assert [row[0] for row in seen] == ["GET", "POST"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_smoke_loopback_redirects_never_contact_destination(status):
    with server() as (target, target_seen), server(status=status, location=target) as (url, _):
        with patch.object(urllib.request, "_opener", None), \
             patch("urllib.request.getproxies", return_value={}):
            with pytest.raises(SMOKE.SmokeFailure, match=f"HTTP {status}"):
                SMOKE.Client(url, "synthetic-test-token", 2).request("GET", "/redirect")
        assert target_seen == []


def test_generation_loopback_bypasses_system_proxy():
    completion = {"choices": [{"message": {"content": '{"ok":true}'}}]}
    with server(payload={"proxy": True}) as (proxy, proxy_seen), server(payload=completion) as (url, seen):
        with patch.object(urllib.request, "_opener", None), \
             patch("urllib.request.getproxies", return_value={"http": proxy}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            client = OpenAICompatibleClient(base_url=url, model="synthetic-model", api_key="", timeout=2)
            content, _ = client.complete_json(system_prompt="Synthetic test", payload={"sample": 1},
                                             stage="test", max_tokens=8)
            assert json.loads(content) == {"ok": True}
        assert proxy_seen == []
        assert len(seen) == 1


def test_generation_health_bypasses_proxy(config_path):
    payload = json.loads(config_path.read_text())
    with server() as (url, seen), server(status=503) as (proxy, proxy_seen):
        payload["generation"]["byok_engine"]["base_url"] = url + "/v1"
        with patch.object(urllib.request, "_opener", None), \
             patch("urllib.request.getproxies", return_value={"http": proxy}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            assert _generation_health(payload)
        assert seen[0][1] == "/v1/health"
        assert proxy_seen == []


def test_generation_probe_rejects_redirect(config_path):
    with server() as (target, target_seen), server(status=303, location=target) as (url, _):
        payload = json.loads(config_path.read_text())
        payload["generation"]["byok_engine"]["base_url"] = url + "/v1"
        config_path.write_text(json.dumps(payload))
        with patch.object(urllib.request, "_opener", None), \
             patch("urllib.request.getproxies", return_value={}):
            from tmcra_local.runtime_env import LocalRuntimeEnvironmentError
            with pytest.raises(LocalRuntimeEnvironmentError, match="HTTP 303"):
                probe_generation_engine(config_path, timeout_seconds=2)
        assert target_seen == []


def test_remote_provider_keeps_existing_transport():
    request = urllib.request.Request("https://provider.example.invalid/v1")
    with patch("urllib.request.urlopen") as remote:
        assert open_request(request, timeout=3) is remote.return_value
        remote.assert_called_once_with(request, timeout=3)


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:2009", True), ("http://127.0.0.2", True),
    ("http://[::1]", True), ("https://localhost", True),
    ("https://example.invalid", False), ("http://127.0.0.1.example.invalid", False),
    ("http://localhost.example.invalid", False), ("http://127.1", False),
    ("http://2130706433", False), ("file:///tmp/sample", False),
    ("http://192.0.2.1", False),  # public-audit: allow-test-fixture
])
def test_loopback_classification(url, expected):
    assert is_loopback_url(url) is expected
