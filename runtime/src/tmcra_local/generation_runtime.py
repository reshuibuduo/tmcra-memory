from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .http_transport import open_request

from .runtime_env import (
    LocalRuntimeEnvironmentError,
    _generation_contract,
    load_local_runtime_config,
)


GENERATION_PROBE_SCHEMA = "tmcra.local-generation-probe.1"


def ensure_local_loopback_key(path: Path) -> str:
    """Create the llama.cpp loopback credential once without placing it in JSON."""

    target = Path(path).expanduser().resolve()
    if target.is_file():
        value = target.read_text(encoding="utf-8").strip()
        if len(value) < 32:
            raise LocalRuntimeEnvironmentError(
                f"local generation loopback key is invalid: {target}"
            )
        return value
    target.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    fd, name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, target)
        if os.name != "nt":
            target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def build_llama_server_command(payload: Mapping[str, Any]) -> list[str]:
    """Build an argv-only command; the credential is referenced by file path."""

    contract = _generation_contract(payload, require_runtime=True)
    if contract["source"] != "local-model":
        raise LocalRuntimeEnvironmentError(
            "a llama.cpp command is available only for the official local model"
        )
    engine = payload.get("generation", {}).get("shared_local_engine", {})
    if not isinstance(engine, Mapping):
        raise LocalRuntimeEnvironmentError("shared local generation engine is missing")
    parsed = urlsplit(contract["base_url"])
    if parsed.port is None:
        raise LocalRuntimeEnvironmentError("local generation endpoint must include a port")
    key_file = Path(str(engine.get("api_key_file") or "")).expanduser().resolve()
    return [
        contract["runtime_executable"],
        "--model",
        contract["model_path"],
        "--alias",
        contract["model_id"],
        "--host",
        str(parsed.hostname),
        "--port",
        str(parsed.port),
        "--ctx-size",
        contract["context_tokens"],
        "--n-predict",
        contract["max_output_tokens"],
        "--parallel",
        "1",
        "--jinja",
        "--no-context-shift",
        "--api-key-file",
        str(key_file),
    ]


def _credential(payload: Mapping[str, Any], contract: Mapping[str, str]) -> str:
    if contract["source"] == "local-model":
        engine = payload.get("generation", {}).get("shared_local_engine", {})
        if not isinstance(engine, Mapping):
            return ""
        key_file = Path(str(engine.get("api_key_file") or "")).expanduser().resolve()
        return key_file.read_text(encoding="utf-8").strip() if key_file.is_file() else ""
    engine = payload.get("generation", {}).get("byok_engine", {})
    key_env = str(engine.get("api_key_env") or "") if isinstance(engine, Mapping) else ""
    key = str(os.getenv(key_env) or "").strip() if key_env else ""
    if key or not isinstance(engine, Mapping):
        return key
    key_file_text = str(engine.get("api_key_file") or "").strip()
    if not key_file_text:
        return ""
    key_file = Path(key_file_text).expanduser().resolve()
    return key_file.read_text(encoding="utf-8").strip() if key_file.is_file() else ""


def probe_generation_engine(
    config_path: Path,
    *,
    timeout_seconds: float = 60.0,
    opener: Any | None = None,
) -> dict[str, Any]:
    """Run one real, provider-neutral OpenAI-compatible JSON generation."""

    source = Path(config_path).expanduser().resolve()
    payload = load_local_runtime_config(source)
    from .runtime_modes import runtime_mode, RuntimeModeError
    mode = runtime_mode(payload)
    if mode != 'enrich':
        raise RuntimeModeError(mode, 'generation probe')
    contract = _generation_contract(payload, require_runtime=False)
    credential = _credential(payload, contract)
    if not credential:
        raise LocalRuntimeEnvironmentError(
            "generation credential is unavailable; set the BYOK environment variable "
            "or create the local loopback key"
        )
    nonce = secrets.token_hex(8)
    body = {
        "model": contract["model_id"],
        "messages": [
            {
                "role": "system",
                "content": "Return exactly one JSON object and no prose.",
            },
            {
                "role": "user",
                "content": (
                    "/no_think\nReturn this object exactly: "
                    + json.dumps(
                        {"tmcra_probe": "ok", "nonce": nonce},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
    }
    encoded = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    request_sha256 = hashlib.sha256(encoded).hexdigest()
    request = urllib.request.Request(
        f"{contract['base_url']}/chat/completions",
        data=encoded,
        headers={
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with (opener or open_request)(
            request, timeout=max(1.0, float(timeout_seconds))
        ) as response:
            http_status = int(response.getcode())
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LocalRuntimeEnvironmentError(
            f"generation probe returned HTTP {exc.code}"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalRuntimeEnvironmentError(
            f"generation probe transport failed: {type(exc).__name__}"
        ) from exc
    choices = response_payload.get("choices") if isinstance(response_payload, Mapping) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        raise LocalRuntimeEnvironmentError(
            "generation probe response must contain exactly one choice"
        )
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise LocalRuntimeEnvironmentError("generation probe response has no text content")
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LocalRuntimeEnvironmentError(
            "generation probe did not return the requested JSON object"
        ) from exc
    if parsed_content != {"tmcra_probe": "ok", "nonce": nonce}:
        raise LocalRuntimeEnvironmentError(
            "generation probe returned a mismatched JSON object"
        )
    usage = response_payload.get("usage") if isinstance(response_payload, Mapping) else None
    safe_usage = {
        key: int(value)
        for key, value in dict(usage or {}).items()
        if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    }
    return {
        "schema_version": GENERATION_PROBE_SCHEMA,
        "status": "passed",
        "source": contract["source"],
        "provider": contract["provider"],
        "profile_id": contract["profile_id"],
        "model": contract["model_id"],
        "http_status": http_status,
        "latency_seconds": round(time.monotonic() - started, 3),
        "request_sha256": request_sha256,
        "response_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "usage": safe_usage,
        "credential_exposed": False,
    }


def _generation_health(
    payload: Mapping[str, Any], *, timeout_seconds: float = 2.0
) -> bool:
    contract = _generation_contract(payload, require_runtime=False)
    credential = _credential(payload, contract)
    headers = {"Accept": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(
        f"{contract['base_url']}/health", headers=headers, method="GET"
    )
    try:
        with open_request(
            request, timeout=max(0.25, float(timeout_seconds))
        ) as response:
            return 200 <= int(response.getcode()) < 300
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


@contextmanager
def managed_local_generation(
    config_path: Path, *, startup_timeout_seconds: float = 300.0, runtime_mode_override=None
):
    """Start and own llama-server only when the selected policy is local-model."""

    source = Path(config_path).expanduser().resolve()
    payload = load_local_runtime_config(source)
    from .runtime_modes import runtime_mode, RuntimeModeError
    mode = runtime_mode(payload, runtime_mode_override)
    if mode == 'off':
        raise RuntimeModeError(mode, 'service startup')
    if mode == 'read-only':
        yield {'source': 'disabled', 'managed': False, 'already_running': False, 'runtime_mode': mode}
        return
    contract = _generation_contract(payload, require_runtime=False)
    if contract["source"] != "local-model":
        yield {"source": contract["source"], "managed": False, "already_running": False}
        return
    engine = payload.get("generation", {}).get("shared_local_engine", {})
    if not isinstance(engine, Mapping):
        raise LocalRuntimeEnvironmentError("shared local generation engine is missing")
    key_file = Path(str(engine.get("api_key_file") or "")).expanduser().resolve()
    ensure_local_loopback_key(key_file)
    if _generation_health(payload):
        yield {"source": "local-model", "managed": False, "already_running": True}
        return
    command = build_llama_server_command(payload)
    creationflags = 0
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        shell=False,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + max(5.0, float(startup_timeout_seconds))
    try:
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                raise LocalRuntimeEnvironmentError(
                    f"llama-server exited during startup with code {return_code}"
                )
            if _generation_health(payload):
                break
            time.sleep(0.5)
        else:
            raise LocalRuntimeEnvironmentError(
                "llama-server did not become healthy before the startup timeout"
            )
        yield {"source": "local-model", "managed": True, "already_running": False}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
