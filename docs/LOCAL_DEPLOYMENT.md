# Local deployment

TMCRA Local is an owner-controlled process. Its API, databases, embedding index, released graph scorers, usage ledger, and generated knowledge documents live on the user's machine. The only optional outbound traffic during normal operation is the OpenAI-compatible generation endpoint explicitly configured by the user.

## Prerequisites

### Loopback transport maintenance

Python generation calls/probes and the local API smoke client bypass proxy
settings for HTTP(S) loopback addresses, including proxies discovered from the
operating system. They reject redirects, even to another local port. Shared
Agent hooks also reject redirects. Configure the final endpoint directly if a
local reverse proxy returns 3xx. Explicit remote BYOK requests retain their
existing proxy behavior; no global network settings are changed.

These boundaries are covered by synthetic loopback-server regression tests,
not by a real-device or real-model acceptance result. The maintenance does not
install an edge vision/audio model or change memory schemas.

### Required tools

- Windows 10/11, a current Linux distribution, or macOS.
- Python 3.12 exactly.
- Git and Git LFS.
- 8 GiB RAM minimum for the default BYOK profile; 16 GiB is recommended.
- Several GiB of free disk for PyTorch, the graph weights, and the selected embedding model.

The installer fails closed if Git LFS leaves pointer files, model manifests do not match, a provider key is missing, or a selected model cannot run a real inference probe.

## BYOK installation

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-local.ps1
```

Linux/macOS:

```bash
bash scripts/install-local.sh
```

The provider must expose an OpenAI-compatible `POST /v1/chat/completions` endpoint and support JSON-object responses. Use a model with at least a 32K context window for normal project memory. The installation probe performs one small JSON completion unless explicitly skipped.

Non-interactive Windows example:

```powershell
$env:MY_PROVIDER_KEY = '<set in this process only>'
powershell -ExecutionPolicy Bypass -File .\scripts\install-local.ps1 `
  -NonInteractive -Mode byok -Provider openai-compatible `
  -BaseUrl https://provider.example/v1 -Model your-model-id `
  -ApiKeyEnv MY_PROVIDER_KEY
Remove-Item Env:MY_PROVIDER_KEY
```

Non-interactive shell example:

```bash
TMCRA_BYOK_BASE_URL=https://provider.example/v1 \
TMCRA_BYOK_MODEL=your-model-id \
TMCRA_BYOK_API_KEY="$MY_PROVIDER_KEY" \
bash scripts/install-local.sh
```

The runtime JSON stores only the key-file path. The key value is written to `.tmcra/config/runtime/secrets/byok-api.key`. The installer makes the repository-local `.tmcra/` tree owner-only (`0700` on POSIX; an inheritance-free owner ACL on Windows), and the runtime reapplies owner-only permissions to config, credentials, and state. The resolved provider key remains on the in-process model client and is not exported to child-process environment variables.

## Model and device profiles

List policies without downloading:

```bash
.tmcra/venv/bin/python -m tmcra_local models
.tmcra/venv/bin/python -m tmcra_local recommend --ram-gib 16 --vram-gib 0 --language multilingual
```

Stable embedding profiles:

| Profile | Intended use |
| --- | --- |
| `compact-zh` | Lower-memory Chinese-first installation |
| `balanced-multilingual` | Default multilingual installation |
| `enhanced-multilingual` | Larger local embedding profile for machines with more memory |

The default reranker is `local-dense-only`. Other reranker options are exposed only when their own download and inference contracts are satisfied; no model-card score is presented as a TMCRA system result.

Windows GPU selection is controlled with `-Torch cpu`, `-Torch cu128`, or `-Torch skip`. Shell installation uses `TMCRA_TORCH_CHANNEL=cpu|cu128|skip`. `auto` selects CUDA only when `nvidia-smi` is available.

## Fully local generation

Local generation requires a compatible `llama-server` executable. TMCRA starts it on `127.0.0.1:2010`, passes the model by absolute path, uses a key file rather than a command-line secret, waits for health, and terminates only the process it started.

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-local.ps1 `
  -Mode local-model `
  -GenerationRuntimeExecutable C:\path\to\llama-server.exe `
  -AcceptLargeModel
```

Shell:

```bash
TMCRA_INSTALL_MODE=local-model \
TMCRA_LLAMA_SERVER=/path/to/llama-server \
TMCRA_ACCEPT_LARGE_MODEL=1 \
bash scripts/install-local.sh
```

The recommended full-quality local profile is configured for 32K context. It downloads approximately 12.74 GiB, so BYOK is the practical default for smaller machines.

## Validate and start

```bash
.tmcra/venv/bin/python -m tmcra_local doctor \
  --config .tmcra/config/runtime/local-runtime.json \
  --probe-models --probe-generation

bash scripts/start-local.sh
```

Windows uses `.tmcra\venv\Scripts\python.exe` and `scripts\start-local.ps1`.

Expected endpoints after startup:

- `GET http://127.0.0.1:2009/v1/health`
- OpenAPI UI at `http://127.0.0.1:2009/docs`

No public-network bind option exists in the stable local runtime.

With the API running, execute the destructive disposable-project smoke test:

```bash
.tmcra/venv/bin/python scripts/smoke_local_api.py
```

Windows:

```powershell
.\.tmcra\venv\Scripts\python.exe .\scripts\smoke_local_api.py
```

The script checks loopback authentication, separate user/assistant writes,
global plus project recall, provenance, Visual Atlas, Personal Knowledge, the
local usage ledger, message erasure, and project erasure. It creates a unique
temporary project and removes it in `finally`. Writer and enabled Personal
Knowledge operations may consume BYOK provider tokens; recall uses only local
retrieval and does not call the provider model. The bearer value is read from
the local secret file and is never printed or accepted as a command-line argument.
By default the test requires model-generated, evidence-cited Personal Knowledge.
Add `--allow-knowledge-fallback` only if you deliberately disabled that optional
task; release acceptance must not use the flag.

After the smoke test passes, connect a supported host through
[Local tool integrations](LOCAL_INTEGRATIONS.md). Codex has a one-command
installer. DeepSeek Harness is a tested technical preview. Claude Code and
ZCode currently provide manual hook manifests without a one-click claim.

## State and backup

The repository-local `.tmcra/` directory contains the environment, model downloads, config, credentials, and SQLite state. It is ignored by Git. Stop TMCRA before making a filesystem backup so the database and WAL are consistent.

A TMCRA delete request compacts the live SQLite databases. Copies in external backups, snapshots, or provider retention systems must be handled separately.

## Uninstall

The default uninstall removes the replaceable Python environment and preserves memory, models, config, and credentials:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-local.ps1
```

```bash
bash scripts/uninstall-local.sh
```

To erase every local TMCRA file under this clone, use `-PurgeData` on Windows or `--purge-data` in the shell script. Both scripts validate their target before recursive deletion.
