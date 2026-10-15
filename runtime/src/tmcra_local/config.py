from __future__ import annotations

import json
import ipaddress
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .auth import restrict_owner_access, restrict_owner_directory
from .model_catalog import (
    LLM_POLICIES,
    generation_model_directory,
    hf_download_command,
    hf_generation_download_command,
    hf_generation_verify_command,
    hf_reranker_download_command,
    hf_reranker_verify_command,
    hf_verify_command,
    model_directory,
    reranker_model_directory,
    resolve_generation_task_policies,
    resolve_embedding_profile,
    resolve_generation_profile,
    resolve_reranker_profile,
)


LOCAL_CONFIG_SCHEMA_VERSION = "tmcra.local-runtime-config.4"


class LocalConfigError(ValueError):
    pass


def _validate_byok_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or parsed.path.rstrip("/") != "/v1"
    ):
        raise LocalConfigError(
            "generation base URL must be a credential-free HTTP(S) /v1 URL"
        )
    if parsed.scheme == "http":
        hostname = parsed.hostname.lower()
        loopback = hostname == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise LocalConfigError("plain HTTP BYOK endpoints must use an exact loopback host")
    return normalized


def default_config_root() -> Path:
    override = os.getenv("TMCRA_LOCAL_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if os.name == "nt":
        app_data = os.getenv("APPDATA", "").strip()
        return (Path(app_data) if app_data else home / "AppData" / "Roaming") / "TMCRA"
    xdg = os.getenv("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg).expanduser() if xdg else home / ".config") / "tmcra"


def default_models_root(config_root: Path | None = None) -> Path:
    override = os.getenv("TMCRA_LOCAL_MODELS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = Path(config_root or default_config_root()).expanduser().resolve()
    return root / "models"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    restrict_owner_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            restrict_owner_access(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    finally:
        temporary.unlink(missing_ok=True)


def build_local_config(
    *,
    embedding_profile_id: str,
    llm_policy_id: str,
    config_root: Path | None = None,
    models_root: Path | None = None,
    byok_provider: str = "",
    byok_base_url: str = "",
    byok_model: str = "",
    byok_api_key_env: str = "",
    byok_api_key_file: str = "",
    generation_task_overrides: dict[str, str] | None = None,
    generation_task_model_overrides: dict[str, str] | None = None,
    embedding_device: str = "auto",
    reranker_profile_id: str = "local-dense-only",
    generation_profile_id: str = "recommended-qwen36",
    local_generation_base_url: str = "http://127.0.0.1:2010/v1",
    generation_runtime_executable: str = "",
    runtime_mode: str = "enrich",
) -> dict[str, Any]:
    if runtime_mode not in {"off", "read-only", "enrich"}:
        raise LocalConfigError("runtime_mode must be off, read-only, or enrich")
    profile = resolve_embedding_profile(embedding_profile_id)
    reranker_profile = resolve_reranker_profile(reranker_profile_id)
    generation_profile = resolve_generation_profile(generation_profile_id)
    known_policies = {str(item["id"]): item for item in LLM_POLICIES}
    if llm_policy_id not in known_policies:
        raise LocalConfigError(f"unknown LLM policy: {llm_policy_id!r}")
    if llm_policy_id not in {"local-model", "byok"}:
        raise LocalConfigError(
            "a complete local runtime must use a configured local model or BYOK"
        )
    task_policies = resolve_generation_task_policies(
        default_policy_id=llm_policy_id,
        overrides=generation_task_overrides,
    )
    for task_id, task_policy in task_policies.items():
        policy_id = str(task_policy.get("policy_id") or "")
        if policy_id == "disabled" and task_id == "personal_knowledge":
            continue
        if policy_id != llm_policy_id:
            raise LocalConfigError(
                "all enabled memory-processing tasks must share the selected "
                f"generation source; {task_id!r} uses {policy_id!r}"
            )
    supplied_task_models = {
        str(key).strip(): str(value).strip()
        for key, value in (generation_task_model_overrides or {}).items()
        if str(value or "").strip()
    }
    unknown_task_models = sorted(set(supplied_task_models) - set(task_policies))
    if unknown_task_models:
        raise LocalConfigError(
            "unknown generation task model override(s): "
            + ", ".join(unknown_task_models)
        )
    if llm_policy_id == "local-model" and supplied_task_models:
        raise LocalConfigError(
            "the official local model is shared; per-task model overrides are not supported"
        )
    if llm_policy_id == "byok" and any(
        model_id != str(byok_model or "").strip()
        for model_id in supplied_task_models.values()
    ):
        raise LocalConfigError(
            "BYOK uses one shared model; per-task model overrides must match byok_model"
        )
    needs_byok = llm_policy_id == "byok"
    if needs_byok:
        missing = [
            name
            for name, value in (
                ("byok_provider", byok_provider),
                ("byok_base_url", byok_base_url),
                ("byok_model", byok_model),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise LocalConfigError(f"BYOK policy requires: {', '.join(missing)}")
    if byok_api_key_env and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", byok_api_key_env
    ):
        raise LocalConfigError("byok_api_key_env must be an environment variable name, not a secret value")
    normalized_byok_base_url = (
        _validate_byok_base_url(byok_base_url) if needs_byok else ""
    )
    task_models: dict[str, dict[str, str]] = {}
    for task_id, task_policy in task_policies.items():
        policy_id = str(task_policy["policy_id"])
        if policy_id == "disabled":
            model_id = ""
            source = "disabled"
        elif policy_id == "local-model":
            model_id = generation_profile.model_alias
            source = "shared-local-runtime"
        elif policy_id == "host-model":
            model_id = "host-current"
            source = "host-runtime"
        else:
            model_id = supplied_task_models.get(task_id) or str(byok_model or "").strip()
            if not model_id:
                raise LocalConfigError(
                    f"generation task {task_id!r} uses BYOK but has no model id"
                )
            source = "task-override" if task_id in supplied_task_models else "byok-default"
        task_models[task_id] = {
            "task_id": task_id,
            "model_id": model_id,
            "selection_source": source,
            "effect_status": (
                generation_profile.effect.status
                if source == "shared-local-runtime"
                else
                "host-dependent"
                if source == "host-runtime"
                else "user-declared;tmcra-ab-unknown"
                if policy_id != "disabled"
                else "disabled"
            ),
        }

    resolved_config_root = Path(config_root or default_config_root()).expanduser().resolve()
    resolved_models_root = Path(models_root or default_models_root(resolved_config_root)).expanduser().resolve()
    normalized_device = str(embedding_device or "auto").strip().lower()
    if normalized_device not in {"auto", "cpu", "cuda", "mps"}:
        raise LocalConfigError("embedding_device must be auto, cpu, cuda, or mps")
    destination = model_directory(resolved_models_root, profile)
    generation_destination = generation_model_directory(
        resolved_models_root, generation_profile
    )
    reranker_destination = reranker_model_directory(
        resolved_models_root, reranker_profile
    )
    reranker_mode = str(reranker_profile.get("runtime_mode") or "").strip()
    checkpoint_path = (
        Path(__file__).resolve().parents[3]
        / "models"
        / "tmcra_v4_longmemeval_s500_20260715"
        / "tmcra_v3_reranker.pt"
    ).resolve()
    reranker_model = reranker_profile.get("model")
    if not isinstance(reranker_model, dict):
        reranker_model = {}
    embedding_weight_bytes = int(profile.weight_bytes)
    reranker_weight_bytes = int(
        reranker_profile.get("approx_weight_bytes") or 0
    )
    uses_local_generation = any(
        str(item.get("policy_id") or "") == "local-model"
        for item in task_policies.values()
    )
    generation_weight_bytes = (
        int(generation_profile.weight_bytes) if uses_local_generation else 0
    )
    normalized_local_generation_url = _validate_byok_base_url(
        local_generation_base_url
    )
    if urlsplit(normalized_local_generation_url).scheme != "http":
        raise LocalConfigError("the bundled local generation endpoint must use loopback HTTP")
    runtime_executable = str(generation_runtime_executable or "").strip()
    if runtime_executable:
        runtime_executable = str(Path(runtime_executable).expanduser().resolve())
    else:
        runtime_executable = str(
            (
                resolved_config_root
                / "runtime"
                / ("llama-server.exe" if os.name == "nt" else "llama-server")
            ).resolve()
        )
    local_key_file = (
        resolved_config_root
        / "runtime"
        / "secrets"
        / "local-generation-api.key"
    ).resolve()
    resolved_byok_key_file = (
        Path(byok_api_key_file).expanduser().resolve()
        if str(byok_api_key_file or "").strip()
        else (resolved_config_root / "runtime" / "secrets" / "byok-api.key").resolve()
    )
    return {
        "schema_version": LOCAL_CONFIG_SCHEMA_VERSION,
        "mode": "local-first",
        "runtime_mode": runtime_mode,
        "network": {
            "bind_host": "127.0.0.1",
            "bind_port": 2009,
            "allow_non_loopback": False,
            "silent_cloud_fallback": False,
        },
        "embedding": {
            "profile_id": profile.id,
            "model_path": str(destination),
            "hf_repo": profile.hf_repo,
            "revision": profile.revision,
            "dimension": profile.dimension,
            "pooling": profile.pooling,
            "query_prefix": profile.query_prefix,
            "document_prefix": profile.document_prefix,
            "max_length": profile.max_length,
            "subchunk_chars": profile.subchunk_chars,
            "strict_no_truncation": True,
            "device_preference": normalized_device,
            "index_signature": profile.index_signature,
            "requires_index_rebuild_on_change": True,
        },
        "reranker": {
            "profile_id": str(reranker_profile["id"]),
            "runtime_mode": reranker_mode,
            "model_path": (
                "" if reranker_destination is None else str(reranker_destination)
            ),
            "hf_repo": str(reranker_model.get("hf_repo") or ""),
            "revision": str(reranker_model.get("revision") or ""),
            "checkpoint_path": str(checkpoint_path) if reranker_mode == "fusion" else "",
            "requires_cross_model": reranker_destination is not None,
            "requires_fusion_checkpoint": reranker_mode == "fusion",
            "validation_status": str(
                reranker_profile.get("validation_status") or ""
            ),
            "effect": dict(reranker_profile.get("effect") or {}),
        },
        "llm": {
            "policy_id": llm_policy_id,
            "task_policies": task_policies,
            "task_models": task_models,
            "byok": {
                "provider": str(byok_provider or "").strip(),
                "base_url": normalized_byok_base_url,
                "model": str(byok_model or "").strip(),
                "api_key_env": str(byok_api_key_env or "").strip(),
                "api_key_file": str(resolved_byok_key_file) if needs_byok else "",
            },
            "secrets_stored_in_config": False,
        },
        "generation": {
            "complete_memory_required": True,
            "retrieval_only_is_developer_mode": True,
            "default_source": llm_policy_id,
            "shared_local_engine": {
                "profile_id": generation_profile.id,
                "provider": "local-qwen",
                "runtime": "llama.cpp",
                "runtime_executable": runtime_executable,
                "base_url": normalized_local_generation_url,
                "model_id": generation_profile.model_alias,
                "model_path": str(
                    (generation_destination / generation_profile.filename).resolve()
                ),
                "model_root": str(generation_destination),
                "hf_repo": generation_profile.hf_repo,
                "revision": generation_profile.revision,
                "filename": generation_profile.filename,
                "weight_bytes": generation_profile.weight_bytes,
                "recommended_ram_gib": generation_profile.recommended_ram_gib,
                "recommended_device": generation_profile.recommended_device,
                "context_tokens": generation_profile.configured_context_tokens,
                "native_context_tokens": generation_profile.native_context_tokens,
                "max_output_tokens": generation_profile.max_output_tokens,
                "api_key_file": str(local_key_file),
                "loopback_only": True,
                "validation_status": generation_profile.validation_status,
            },
            "byok_engine": {
                "provider": "openai-compatible",
                "provider_name": str(byok_provider or "").strip(),
                "base_url": normalized_byok_base_url,
                "model_id": str(byok_model or "").strip(),
                "api_key_env": str(byok_api_key_env or "").strip(),
                "api_key_file": str(resolved_byok_key_file) if needs_byok else "",
                "context_tokens": 32_768,
                "max_output_tokens": 16_384,
                "secret_stored_in_config": False,
            },
            "task_routes": task_policies,
        },
        "installation": {
            "config_root": str(resolved_config_root),
            "models_root": str(resolved_models_root),
            "download_command": hf_download_command(profile, destination),
            "verify_command": hf_verify_command(profile, destination),
            "model_downloads": [
                {
                    "component": "embedding",
                    "profile_id": profile.id,
                    "destination": str(destination),
                    "download_command": hf_download_command(profile, destination),
                    "verify_command": hf_verify_command(profile, destination),
                },
                *(
                    []
                    if reranker_destination is None
                    else [
                        {
                            "component": "reranker",
                            "profile_id": str(reranker_profile["id"]),
                            "destination": str(reranker_destination),
                            "download_command": hf_reranker_download_command(
                                reranker_profile, reranker_destination
                            ),
                            "verify_command": hf_reranker_verify_command(
                                reranker_profile, reranker_destination
                            ),
                        }
                    ]
                ),
                *(
                    []
                    if not uses_local_generation
                    else [
                        {
                            "component": "generation",
                            "profile_id": generation_profile.id,
                            "destination": str(generation_destination),
                            "download_command": hf_generation_download_command(
                                generation_profile, generation_destination
                            ),
                            "verify_command": hf_generation_verify_command(
                                generation_profile, generation_destination
                            ),
                        }
                    ]
                ),
            ],
            "resource_plan": {
                "embedding_weight_bytes": embedding_weight_bytes,
                "reranker_weight_bytes": reranker_weight_bytes,
                "generation_weight_bytes": generation_weight_bytes,
                "total_selected_weight_bytes": (
                    embedding_weight_bytes
                    + reranker_weight_bytes
                    + generation_weight_bytes
                ),
                "total_selected_weight_gib": round(
                    (
                        embedding_weight_bytes
                        + reranker_weight_bytes
                        + generation_weight_bytes
                    )
                    / 1024**3,
                    2,
                ),
                "generation_weights_local": uses_local_generation,
                "generation_source_note": (
                    "The official local mode downloads one shared generation model. "
                    "BYOK does not download generation weights."
                ),
            },
        },
    }


def write_local_config(payload: dict[str, Any], *, config_root: Path | None = None) -> Path:
    root = Path(config_root or payload.get("installation", {}).get("config_root") or default_config_root())
    path = root.expanduser().resolve() / "runtime" / "local-runtime.json"
    _atomic_json(path, payload)
    return path
