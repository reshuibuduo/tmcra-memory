"""Explicit local execution collector. Not reachable through the public API.

Example: python -m tmcra_local.workflow_cli --db STATE --namespace PROJECT
 --task TASK --operation-id RECEIPT --version COMMIT --timeout 60 -- python -m pytest

Only exit status, timestamps and output hashes persist. Command arguments,
working-directory paths and stdout/stderr plaintext are never saved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import threading
from datetime import datetime, timezone

from .workflows import WorkflowError, WorkflowStore, _EXECUTION_CAPABILITY, _iso


def record_command(store: WorkflowStore, namespace: str, task_id: str, command: list[str], *, operation_id: str, version: str, timeout: float = 60, cwd: str | None = None) -> dict:
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command) or len(command) > 128 or sum(map(len, command)) > 32768:
        raise WorkflowError("invalid_command", "A bounded argument list is required")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 600:
        raise WorkflowError("invalid_timeout", "Execution timeout must be between 0.1 and 600 seconds")
    if not version or len(version) > 256:
        raise WorkflowError("invalid_version", "An explicit version is required for execution receipts")
    # Reject missing tasks and consumed operation IDs before running a command.
    from .workflows import _identifier
    _identifier(namespace, "namespace")
    _identifier(task_id, "task_id")
    _identifier(operation_id, "operation_id")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT body FROM workflow_records WHERE namespace=? AND kind='task' AND id=? AND deleted=0", (namespace, task_id)).fetchone()
        if not task:
            raise WorkflowError("record_not_found", "Task does not exist", 404)
        task_revision = json.loads(task["body"]).get("definition_revision")
        if db.execute("SELECT 1 FROM workflow_command_claims WHERE namespace=? AND operation_id=?", (namespace, operation_id)).fetchone():
            raise WorkflowError("operation_conflict", "Execution receipt ID already reserved; command was not run", 409)
        if db.execute("SELECT 1 FROM workflow_operations WHERE namespace=? AND operation_id=?", (namespace, operation_id)).fetchone():
            raise WorkflowError("operation_conflict", "Execution receipt ID already used; command was not run", 409)
        db.execute("INSERT INTO workflow_command_claims VALUES(?,?)", (namespace, operation_id))
    started = _iso(datetime.now(timezone.utc))
    digests = [hashlib.sha256(), hashlib.sha256()]
    timed_out = False
    try:
        process = subprocess.Popen(command, cwd=cwd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == "posix")
    except OSError:
        # Do not persist exception text, which may contain private paths/arguments.
        exit_code = 127
    else:
        def drain(pipe, digest):
            with pipe:
                while block := pipe.read(65536):
                    digest.update(block)
        threads = [threading.Thread(target=drain, args=(pipe, digest), daemon=True) for pipe, digest in zip((process.stdout, process.stderr), digests)]
        for thread in threads:
            thread.start()
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            exit_code = process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            # Descendants can inherit pipes even after the parent exits. Close the
            # original process group so collection has a bounded lifetime.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for thread in threads:
                thread.join(timeout=1)
            timed_out = True
    finished = _iso(datetime.now(timezone.utc))
    return store._execute(namespace, "_task.executed", {"id": task_id, "operation_id": operation_id, "version": version,
        "summary": "Explicit local command execution", "outcome": "passed" if exit_code == 0 and not timed_out else "failed", "evidence_ids": [],
        "exit_code": exit_code, "timed_out": timed_out, "stdout_sha256": digests[0].hexdigest(), "stderr_sha256": digests[1].hexdigest(),
        "started_at": started, "finished_at": finished, "command_kind": "explicit_local_command", "task_revision": task_revision}, capability=_EXECUTION_CAPABILITY)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        receipt = record_command(WorkflowStore(args.db), args.namespace, args.task, command, operation_id=args.operation_id, version=args.version, timeout=args.timeout)
    except WorkflowError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}))
        return 2
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt["record"]["receipts"][-1]["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
