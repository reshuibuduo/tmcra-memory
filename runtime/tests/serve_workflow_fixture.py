"""Loopback browser acceptance fixture. Uses real SQLite/API; only LLM is fake.

This creates isolated test data under --state-dir. No production config, account,
model or provider is read. Ctrl-C stops the fixture; delete its directory after use.
"""
import argparse
from pathlib import Path
import uvicorn
from conftest import local_config, build_test_engine
from tmcra_local import api

parser = argparse.ArgumentParser()
parser.add_argument("--state-dir", required=True)
parser.add_argument("--port", type=int, default=18743)
args = parser.parse_args()
root = Path(args.state_dir).resolve()
root.mkdir(parents=True, exist_ok=True)
config = local_config(root)
engine = build_test_engine(config)
engine.ingest_message(project_id="demo", session_id="demo-session", role="user", content="Memory test source: choose bounded retries.",
    source_app="test", native_thread_id="demo-session", native_message_id="fixture-source", message_id="fixture-source")
api.LocalMemoryEngine = lambda *a, **k: engine
app = api.create_app(config, verify_models=False)
print("FIXTURE_READY", flush=True)
uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
