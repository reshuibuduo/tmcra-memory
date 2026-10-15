from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import build_local_config, default_config_root, default_models_root, write_local_config
from .model_catalog import (
    CATALOG_SCHEMA_VERSION,
    catalog_payload,
    hf_download_command,
    hf_generation_download_command,
    hf_generation_verify_command,
    hf_reranker_download_command,
    hf_reranker_verify_command,
    hf_verify_command,
    model_directory,
    generation_model_directory,
    reranker_model_directory,
    recommend_embedding_profile,
    recommend_reranker_profile,
    resolve_embedding_profile,
    resolve_generation_profile,
    resolve_reranker_profile,
    validate_local_model_files,
    validate_local_generation_files,
    validate_local_reranker_files,
)
from .runtime_env import build_service_environment, load_local_runtime_config


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _emit(payload: dict[str, Any], *, compact: bool = False) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=None if compact else 2, sort_keys=True))


def _secret_free_environment(environment: dict[str, str]) -> dict[str, str]:
    """Return a diagnostic view without serializing provider credentials."""

    secret_names = {
        "TMCRA_WRITER_API_KEY_POOL",
        "TMCRA_WRITER_REVIEWER_API_KEY_POOL",
    }
    return {
        name: ("<configured>" if name in secret_names and value else value)
        for name, value in environment.items()
    }


def _models(args: argparse.Namespace) -> int:
    _emit(catalog_payload(include_preview=args.include_preview), compact=args.json)
    return 0


def _recommend(args: argparse.Namespace) -> int:
    profile = recommend_embedding_profile(
        ram_gib=args.ram_gib,
        vram_gib=args.vram_gib,
        language=args.language,
    )
    reranker = recommend_reranker_profile(
        ram_gib=args.ram_gib,
        vram_gib=args.vram_gib,
        language=args.language,
    )
    _emit(
        {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "recommendation": profile.as_public_dict(),
            "stack_recommendation": {
                "embedding": profile.as_public_dict(),
                "reranker": reranker,
            },
            "automatic_download": False,
        },
        compact=args.json,
    )
    return 0


def _plan(args: argparse.Namespace) -> int:
    root = Path(args.models_root or default_models_root()).expanduser().resolve()
    if args.embedding:
        profile = resolve_embedding_profile(
            args.embedding, allow_preview=args.allow_preview
        )
        public_profile = profile.as_public_dict()
        destination = model_directory(root, profile)
        download_command = hf_download_command(profile, destination)
        verify_command = hf_verify_command(profile, destination)
        component = "embedding"
    elif args.reranker:
        profile = resolve_reranker_profile(
            args.reranker, allow_preview=args.allow_preview
        )
        public_profile = dict(profile)
        destination = reranker_model_directory(root, profile)
        download_command = (
            []
            if destination is None
            else hf_reranker_download_command(profile, destination)
        )
        verify_command = (
            []
            if destination is None
            else hf_reranker_verify_command(profile, destination)
        )
        component = "reranker"
    else:
        profile = resolve_generation_profile(
            args.generation, allow_preview=args.allow_preview
        )
        public_profile = profile.as_public_dict()
        destination = generation_model_directory(root, profile)
        download_command = hf_generation_download_command(profile, destination)
        verify_command = hf_generation_verify_command(profile, destination)
        component = "generation"
    _emit(
        {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "component": component,
            "profile": public_profile,
            "destination": "" if destination is None else str(destination),
            "download_command": download_command,
            "verify_command": verify_command,
            "automatic_download": False,
        },
        compact=args.json,
    )
    return 0


def _configure(args: argparse.Namespace) -> int:
    config_root = Path(args.config_root).expanduser().resolve() if args.config_root else default_config_root()
    models_root = Path(args.models_root).expanduser().resolve() if args.models_root else default_models_root(config_root)
    payload = build_local_config(
        embedding_profile_id=args.embedding,
        reranker_profile_id=args.reranker,
        llm_policy_id=args.llm_policy,
        config_root=config_root,
        models_root=models_root,
        byok_provider=args.byok_provider,
        byok_base_url=args.byok_base_url,
        byok_model=args.byok_model,
        byok_api_key_env=args.byok_api_key_env,
        byok_api_key_file=args.byok_api_key_file,
        embedding_device=args.embedding_device,
        generation_task_overrides={
            "memory_writer": args.writer_policy,
            "personal_knowledge": args.knowledge_policy,
        },
        generation_task_model_overrides={
            "memory_writer": args.writer_model,
            "personal_knowledge": args.knowledge_model,
        },
        generation_profile_id=args.generation_profile,
        local_generation_base_url=args.local_generation_base_url,
        generation_runtime_executable=args.generation_runtime_executable,
        runtime_mode=getattr(args, "runtime_mode", "enrich"),
    )
    if args.dry_run:
        path = config_root / "runtime" / "local-runtime.json"
    else:
        path = write_local_config(payload, config_root=config_root)
        if args.llm_policy == "local-model":
            from .generation_runtime import ensure_local_loopback_key

            key_path = Path(
                payload["generation"]["shared_local_engine"]["api_key_file"]
            )
            ensure_local_loopback_key(key_path)
    _emit(
        {
            "schema_version": payload["schema_version"],
            "status": "planned" if args.dry_run else "configured",
            "config_path": str(path),
            "config": payload,
        },
        compact=args.json,
    )
    return 0


def _download(args: argparse.Namespace) -> int:
    models_root = Path(args.models_root or default_models_root()).expanduser().resolve()
    if args.embedding:
        profile = resolve_embedding_profile(
            args.embedding, allow_preview=args.allow_preview
        )
        component = "embedding"
        profile_id = profile.id
        public_profile = profile.as_public_dict()
        destination = model_directory(models_root, profile)
        download = hf_download_command(profile, destination)
        verify = hf_verify_command(profile, destination)
    elif args.reranker:
        profile = resolve_reranker_profile(
            args.reranker, allow_preview=args.allow_preview
        )
        component = "reranker"
        profile_id = str(profile["id"])
        public_profile = dict(profile)
        destination = reranker_model_directory(models_root, profile)
        if destination is None:
            _emit(
                {
                    "status": "not-required",
                    "component": component,
                    "profile_id": profile_id,
                    "destination": "",
                    "download_command": [],
                    "verify_command": [],
                },
                compact=args.json,
            )
            return 0
        download = hf_reranker_download_command(profile, destination)
        verify = hf_reranker_verify_command(profile, destination)
    else:
        profile = resolve_generation_profile(
            args.generation, allow_preview=args.allow_preview
        )
        component = "generation"
        profile_id = profile.id
        public_profile = profile.as_public_dict()
        destination = generation_model_directory(models_root, profile)
        download = hf_generation_download_command(profile, destination)
        verify = hf_generation_verify_command(profile, destination)
    if not args.execute:
        _emit(
            {
                "status": "planned",
                "component": component,
                "profile_id": profile_id,
                "destination": str(destination),
                "download_command": download,
                "verify_command": verify,
            },
            compact=args.json,
        )
        return 0
    if shutil.which("hf") is None:
        raise RuntimeError("the Hugging Face `hf` CLI is required")
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(download, check=True, shell=False)
    subprocess.run(verify, check=True, shell=False)
    if component == "embedding":
        local_files = validate_local_model_files(profile, destination)
    elif component == "reranker":
        local_files = validate_local_reranker_files(profile, destination)
    else:
        local_files = validate_local_generation_files(profile, destination)
    if not local_files["complete"]:
        raise RuntimeError(f"downloaded model is incomplete: {local_files['required_files']}")
    manifest = {
        "schema_version": "tmcra.local-model-install.1",
        "profile": public_profile,
        "verified_with_hf_cache": True,
        "required_files": local_files["required_files"],
    }
    manifest_path = destination / "TMCRA_MODEL_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _emit(
        {
            "status": "installed",
            "component": component,
            "profile_id": profile_id,
            "destination": str(destination),
            "manifest": str(manifest_path),
        },
        compact=args.json,
    )
    return 0


def _service_env(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    payload = load_local_runtime_config(config_path)
    environment = build_service_environment(
        payload,
        config_path=config_path,
        require_model=not args.allow_missing_model,
    )
    _emit(
        {
            "status": "ready",
            "config_path": str(config_path),
            "environment": _secret_free_environment(environment),
            "contains_secret_values": False,
        },
        compact=args.json,
    )
    return 0


def _probe_models(args: argparse.Namespace) -> int:
    from .inference_probe import probe_local_models

    result = probe_local_models(Path(args.config).expanduser().resolve())
    _emit(result, compact=args.json)
    return 0


def _generation_command(args: argparse.Namespace) -> int:
    from .generation_runtime import build_llama_server_command

    config_path = Path(args.config).expanduser().resolve()
    payload = load_local_runtime_config(config_path)
    _emit(
        {
            "status": "ready",
            "config_path": str(config_path),
            "command": build_llama_server_command(payload),
            "contains_secret_values": False,
        },
        compact=args.json,
    )
    return 0


def _probe_generation(args: argparse.Namespace) -> int:
    from .generation_runtime import probe_generation_engine

    result = probe_generation_engine(
        Path(args.config).expanduser().resolve(),
        timeout_seconds=args.timeout,
    )
    _emit(result, compact=args.json)
    return 0


def _start(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app
    from .generation_runtime import managed_local_generation

    config_path = Path(args.config).expanduser().resolve()
    payload = load_local_runtime_config(config_path)
    with managed_local_generation(
        config_path, startup_timeout_seconds=args.generation_startup_timeout,
        runtime_mode_override=getattr(args, 'runtime_mode', None),
    ):
        app = create_app(config_path, verify_models=not args.allow_missing_model,
                         runtime_mode_override=getattr(args, 'runtime_mode', None))
        environment = app.state.engine.environment
        uvicorn.run(
            app,
            host=environment["TMCRA_SERVICE_BIND_HOST"],
            port=int(environment["TMCRA_SERVICE_BIND_PORT"]),
            log_level=args.log_level,
            access_log=bool(args.access_log),
        )
    return 0


def _token(args: argparse.Namespace) -> int:
    from .auth import ensure_local_token

    config_path = Path(args.config).expanduser().resolve()
    payload = load_local_runtime_config(config_path)
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        raise RuntimeError("local runtime installation config is missing")
    path, value, created = ensure_local_token(installation.get("config_root") or "")
    result = {
        "status": "created" if created else "ready",
        "token_path": str(path),
        "token": value if args.show else "<hidden>",
    }
    _emit(result, compact=args.json)
    return 0


def _set_key(args: argparse.Namespace) -> int:
    from .auth import write_secret_file

    config_path = Path(args.config).expanduser().resolve()
    payload = load_local_runtime_config(config_path)
    llm = payload.get("llm")
    byok = llm.get("byok") if isinstance(llm, dict) else None
    if not isinstance(byok, dict) or not str(byok.get("api_key_file") or ""):
        raise RuntimeError("this config has no BYOK credential file")
    if args.from_env:
        value = str(os.getenv(args.from_env) or "")
        if not value:
            raise RuntimeError(f"credential environment variable is empty: {args.from_env}")
    else:
        value = getpass.getpass("BYOK API key (stored only in the configured local secret file): ")
    path = write_secret_file(str(byok["api_key_file"]), value)
    _emit(
        {"status": "configured", "credential_path": str(path), "secret_printed": False},
        compact=args.json,
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    """Validate the owner-local release without printing credentials."""

    from .auth import ensure_local_token

    config_path = Path(args.config).expanduser().resolve()
    payload = load_local_runtime_config(config_path)
    from .runtime_modes import runtime_mode, read_only_environment, RuntimeModeError
    mode = runtime_mode(payload)
    if mode != 'enrich':
        if args.probe_models or args.probe_generation:
            raise RuntimeModeError(mode, 'model probe')
        environment = read_only_environment(payload, config_path)
        _emit({'status': 'off' if mode == 'off' else 'ready', 'runtime_mode': mode,
               'checks': {'config': 'passed', 'generation': 'disabled', 'model_initialization': 'not_performed'},
               'contains_secret_values': False}, compact=args.json)
        return 0
    environment = build_service_environment(
        payload,
        config_path=config_path,
        require_model=not args.allow_missing_model,
    )
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        raise RuntimeError("local runtime installation config is missing")
    token_file, _, _ = ensure_local_token(installation.get("config_root") or "")
    checks: dict[str, Any] = {
        "config": "passed",
        "loopback_only": environment["TMCRA_SERVICE_BIND_HOST"] == "127.0.0.1",
        "embedding_and_graph_assets": (
            "skipped" if args.allow_missing_model else "passed"
        ),
        "local_api_token": "passed" if token_file.is_file() else "failed",
        "generation_source": environment["TMCRA_LOCAL_GENERATION_SOURCE"],
        "generation_credential": (
            "configured"
            if environment.get("TMCRA_WRITER_API_KEY_POOL")
            else "missing"
        ),
    }
    if checks["generation_credential"] != "configured":
        raise RuntimeError("generation credential is not configured")
    if args.probe_models:
        from .inference_probe import probe_local_models

        checks["model_probe"] = probe_local_models(config_path)
    if args.probe_generation:
        from .generation_runtime import (
            managed_local_generation,
            probe_generation_engine,
        )

        with managed_local_generation(
            config_path, startup_timeout_seconds=args.generation_startup_timeout
        ):
            checks["generation_probe"] = probe_generation_engine(
                config_path, timeout_seconds=args.timeout
            )
    _emit(
        {
            "status": "ready",
            "mode": "owner-local",
            "config_path": str(config_path),
            "checks": checks,
            "contains_secret_values": False,
        },
        compact=args.json,
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="TMCRA local runtime model policy")
    sub = result.add_subparsers(dest="command", required=True)

    models = sub.add_parser("models", help="list selectable model policies")
    models.add_argument("--include-preview", action="store_true")
    models.add_argument("--json", action="store_true")
    models.set_defaults(handler=_models)

    recommend = sub.add_parser("recommend", help="recommend without downloading")
    recommend.add_argument("--ram-gib", type=float, required=True)
    recommend.add_argument("--vram-gib", type=float, default=0.0)
    recommend.add_argument("--language", choices=("zh", "en", "multilingual"), default="multilingual")
    recommend.add_argument("--json", action="store_true")
    recommend.set_defaults(handler=_recommend)

    plan = sub.add_parser("plan-model", help="show a pinned Hugging Face download plan")
    plan_component = plan.add_mutually_exclusive_group(required=True)
    plan_component.add_argument("--embedding")
    plan_component.add_argument("--reranker")
    plan_component.add_argument("--generation")
    plan.add_argument("--models-root", default="")
    plan.add_argument("--allow-preview", action="store_true")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(handler=_plan)

    configure = sub.add_parser("configure", help="write a secret-free local runtime policy")
    configure.add_argument("--mode", dest="runtime_mode", choices=["off", "read-only", "enrich"], default="enrich")
    configure.add_argument("--embedding", required=True)
    configure.add_argument(
        "--reranker",
        choices=(
            "local-dense-only",
            "compact-cross-reranker",
        ),
        default="local-dense-only",
    )
    configure.add_argument(
        "--llm-policy",
        choices=("local-model", "byok"),
        default="local-model",
    )
    configure.add_argument(
        "--generation-profile",
        choices=("recommended-qwen36",),
        default="recommended-qwen36",
    )
    configure.add_argument(
        "--local-generation-base-url",
        default="http://127.0.0.1:2010/v1",
    )
    configure.add_argument("--generation-runtime-executable", default="")
    configure.add_argument("--byok-provider", default="")
    configure.add_argument("--byok-base-url", default="")
    configure.add_argument("--byok-model", default="")
    configure.add_argument("--byok-api-key-env", default="")
    configure.add_argument("--byok-api-key-file", default="")
    configure.add_argument(
        "--embedding-device", choices=("auto", "cpu", "cuda", "mps"), default="auto"
    )
    task_policy_choices = ("inherit", "local-model", "byok")
    configure.add_argument("--writer-policy", choices=task_policy_choices, default="inherit")
    configure.add_argument("--writer-model", default="")
    configure.add_argument(
        "--knowledge-policy",
        choices=(*task_policy_choices, "disabled"),
        default="inherit",
    )
    configure.add_argument("--knowledge-model", default="")
    configure.add_argument("--config-root", default="")
    configure.add_argument("--models-root", default="")
    configure.add_argument("--dry-run", action="store_true")
    configure.add_argument("--json", action="store_true")
    configure.set_defaults(handler=_configure)

    download = sub.add_parser("download-model", help="download and verify a selected model")
    download_component = download.add_mutually_exclusive_group(required=True)
    download_component.add_argument("--embedding")
    download_component.add_argument("--reranker")
    download_component.add_argument("--generation")
    download.add_argument("--models-root", default="")
    download.add_argument("--allow-preview", action="store_true")
    download.add_argument("--execute", action="store_true")
    download.add_argument("--json", action="store_true")
    download.set_defaults(handler=_download)

    service_env = sub.add_parser(
        "service-env", help="resolve a local config into a secret-free service environment"
    )
    service_env.add_argument("--config", required=True)
    service_env.add_argument("--allow-missing-model", action="store_true")
    service_env.add_argument("--json", action="store_true")
    service_env.set_defaults(handler=_service_env)

    probe_models = sub.add_parser(
        "probe-models", help="run real local embedding and reranker tensors"
    )
    probe_models.add_argument("--config", required=True)
    probe_models.add_argument("--json", action="store_true")
    probe_models.set_defaults(handler=_probe_models)

    generation_command = sub.add_parser(
        "generation-command",
        help="render the secret-free llama.cpp command for the selected local profile",
    )
    generation_command.add_argument("--config", required=True)
    generation_command.add_argument("--json", action="store_true")
    generation_command.set_defaults(handler=_generation_command)

    probe_generation = sub.add_parser(
        "probe-generation",
        help="run one real JSON completion against the selected generation engine",
    )
    probe_generation.add_argument("--config", required=True)
    probe_generation.add_argument("--timeout", type=float, default=60.0)
    probe_generation.add_argument("--json", action="store_true")
    probe_generation.set_defaults(handler=_probe_generation)

    start = sub.add_parser("start", help="start the loopback-only TMCRA local API")
    start.add_argument("--config", required=True)
    start.add_argument("--allow-missing-model", action="store_true", help=argparse.SUPPRESS)
    start.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default="info",
    )
    start.add_argument("--access-log", action="store_true")
    start.add_argument("--generation-startup-timeout", type=float, default=300.0)
    start.add_argument("--mode", dest="runtime_mode", choices=["off", "read-only", "enrich"], default=None, help="override runtime mode for this invocation only")
    start.set_defaults(handler=_start)

    token = sub.add_parser("token", help="create or inspect the local API token path")
    token.add_argument("--config", required=True)
    token.add_argument("--show", action="store_true", help="print the token for local client setup")
    token.add_argument("--json", action="store_true")
    token.set_defaults(handler=_token)

    set_key = sub.add_parser("set-key", help="store a BYOK key in the local secret file")
    set_key.add_argument("--config", required=True)
    set_key.add_argument("--from-env", default="")
    set_key.add_argument("--json", action="store_true")
    set_key.set_defaults(handler=_set_key)

    doctor = sub.add_parser(
        "doctor", help="validate local config, assets, credentials, and optional probes"
    )
    doctor.add_argument("--config", required=True)
    doctor.add_argument("--allow-missing-model", action="store_true", help=argparse.SUPPRESS)
    doctor.add_argument("--probe-models", action="store_true")
    doctor.add_argument("--probe-generation", action="store_true")
    doctor.add_argument("--timeout", type=float, default=60.0)
    doctor.add_argument("--generation-startup-timeout", type=float, default=300.0)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=_doctor)
    workflow = sub.add_parser("workflow", help="use the existing local workflow API (no model initialization)", add_help=False)
    workflow.add_argument("workflow_args", nargs=argparse.REMAINDER)
    def workflow_handler(args):
        from .workflow_client import main as workflow_main
        return workflow_main(args.workflow_args)
    workflow.set_defaults(handler=workflow_handler)
    return result


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    selected = list(sys.argv[1:] if argv is None else argv)
    if selected and selected[0] == "workflow":
        from .workflow_client import main as workflow_main
        return workflow_main(selected[1:])
    try:
        args = parser().parse_args(argv)
        return int(args.handler(args))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
