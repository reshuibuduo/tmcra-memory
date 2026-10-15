"""Explicit capability modes. Omission preserves the existing enrich contract."""
from pathlib import Path
from .runtime_env import LocalRuntimeEnvironmentError, _loopback_network


def runtime_mode(payload, override=None):
    value = override if override is not None else payload.get('runtime_mode', 'enrich')
    if value not in {'off', 'read-only', 'enrich'}:
        raise LocalRuntimeEnvironmentError('runtime_mode must be off, read-only, or enrich')
    return value


class RuntimeModeError(RuntimeError):
    def __init__(self, mode, operation):
        self.mode = mode
        self.operation = operation
        super().__init__(f'{operation} is disabled in {mode} runtime mode')


def read_only_environment(payload, config_path):
    # Deliberately never calls generation/model contracts, credential readers,
    # compute-device discovery, embedding imports, or generation health probes.
    host, port = _loopback_network(payload)
    installation = payload.get('installation') or {}
    root = str(installation.get('config_root') or '').strip()
    if not root:
        raise LocalRuntimeEnvironmentError('local runtime installation metadata is missing')
    return {
        'TMCRA_LOCAL_RUNTIME_CONFIG': str(config_path),
        'TMCRA_SERVICE_STATE_DIR': str(Path(root).expanduser().resolve() / 'state'),
        'TMCRA_SERVICE_BIND_HOST': host, 'TMCRA_SERVICE_BIND_PORT': str(port),
        'TMCRA_GRAPH_RETRIEVAL_MODE': 'heuristic',
        'TMCRA_WRITE_EMBEDDER_INDEX_MODE': 'off',
        'TMCRA_EMBEDDER_INDEX_RECALL_MODE': 'off',
        'TMCRA_EMBEDDER_PRE_RECALL_MODE': 'off',
        'TMCRA_EMBEDDER_FUSION_MODE': 'off',
        'TMCRA_RERANKER_MODE': 'off',
        'TMCRA_LOCAL_GENERATION_SOURCE': 'disabled',
        'TMCRA_WRITER_MODEL': '', 'TMCRA_EMBEDDING_PROFILE_ID': 'disabled-read-only',
        'TMCRA_LOCAL_PERSONAL_KNOWLEDGE_ENABLED': '0',
    }
