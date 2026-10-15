import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from tmcra_local.workflow_cli import main, record_command
from tmcra_local.workflows import WorkflowError, WorkflowStore


@pytest.fixture
def store(tmp_path):
    store = WorkflowStore(tmp_path / "workflows.db")
    store.execute("p", "task.upsert", {"id": "t", "title": "test", "version": "v1", "operation_id": "task"})
    return store


def execute(store, code, op="run", version="v1", timeout=5):
    return record_command(store, "p", "t", [sys.executable, "-c", code], operation_id=op, version=version, timeout=timeout)


def test_success_actual_command_and_no_plaintext_secrets(store):
    result = execute(store, "print('secret-output-123')")
    receipt = result["record"]["receipts"][-1]
    assert receipt["exit_code"] == 0
    assert receipt["stdout_sha256"] == hashlib.sha256(b"secret-output-123\n").hexdigest()
    assert receipt["assurance"] == "executed_local"
    assert store.snapshot("p")["tasks"][0]["verified"]
    with store._connect() as db:
        dump = "\n".join(db.iterdump())
    assert "secret-output-123" not in dump
    assert sys.executable not in dump
    assert "not proof" in store.snapshot("p")["tasks"][0]["verification_boundary"]


def test_failed_command_downgrades_old_success(store):
    execute(store, "pass", op="pass")
    execute(store, "import sys;sys.exit(3)", op="fail")
    task = store.snapshot("p")["tasks"][0]
    assert not task["verified"]
    assert task["verification_state"] == "execution_failed"
    assert task["receipts"][-1]["exit_code"] == 3


def test_version_and_expiry_are_not_reused(store):
    execute(store, "pass")
    store.execute("p", "task.upsert", {"id": "t", "version": "v2", "operation_id": "update"})
    assert not store.snapshot("p")["tasks"][0]["verified"]
    store.execute("p", "task.upsert", {"id": "t", "version": "v1", "operation_id": "back"})
    store._now = lambda: datetime.now(timezone.utc) + timedelta(days=2)
    assert store.snapshot("p")["tasks"][0]["verification_state"] == "stale"


def test_command_is_shell_false(store, tmp_path):
    marker = tmp_path / "unwanted"
    command = [sys.executable, "-c", "import sys; print(sys.argv[1])", f"; touch {marker}"]
    record_command(store, "p", "t", command, operation_id="shell", version="v1")
    assert not marker.exists()


def test_timeout_is_recorded_as_failure(store):
    execute(store, "import time;time.sleep(10)", timeout=0.1)
    receipt = store.snapshot("p")["tasks"][0]["receipts"][-1]
    assert receipt["timed_out"]
    assert receipt["outcome"] == "failed"
    assert not store.snapshot("p")["tasks"][0]["verified"]


def test_execution_claim_prevents_duplicate_command(store, tmp_path):
    marker = tmp_path / "count"
    code = f"from pathlib import Path; p=Path({str(marker)!r});p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    def call(_):
        try:
            execute(store, code)
            return "ran"
        except WorkflowError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))
    assert results.count("ran") == 1
    assert marker.read_text() == "x"
    with pytest.raises(WorkflowError):
        execute(store, code)
    assert marker.read_text() == "x"


def test_missing_task_does_not_run(tmp_path):
    marker = tmp_path / "no"
    store = WorkflowStore(tmp_path / "s.db")
    with pytest.raises(WorkflowError):
        execute(store, f"open({str(marker)!r},'w').close()")
    assert not marker.exists()


def test_cli_returns_real_failure(store, capsys):
    code = main(["--db", store.path, "--namespace", "p", "--task", "t", "--operation-id", "cli", "--version", "v1", "--", sys.executable, "-c", "import sys;sys.exit(4)"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["record"]["receipts"][-1]["exit_code"] == 4


@pytest.mark.parametrize("timeout", [0, 601, float("nan"), True])
def test_timeout_bounds(store, timeout):
    with pytest.raises(WorkflowError):
        execute(store, "pass", timeout=timeout)


def test_changing_goal_invalidates_receipt_even_with_same_version(store):
    execute(store, "pass")
    assert store.snapshot("p")["tasks"][0]["verified"]
    store.execute("p", "task.upsert", {"id":"t", "goal":"A different acceptance criterion", "operation_id":"new-goal"})
    assert not store.snapshot("p")["tasks"][0]["verified"]
    execute(store, "pass", op="new-acceptance")
    assert store.snapshot("p")["tasks"][0]["verified"]
