from datetime import datetime, timedelta, timezone
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tmcra_local.workflows import WorkflowError, WorkflowStore


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "workflows.sqlite")


def run(store, action, payload=None, *, ns="project", op=None):
    payload = dict(payload or {})
    payload["operation_id"] = op or f"op{store.snapshot(ns)['revision'] + 1}"
    return store.execute(ns, action, payload)


def task(store, **kwargs):
    return run(store, "task.upsert", {"id": "t", "title": "Ship", "version": "commit-a", **kwargs})


def decision(store, ident, **kwargs):
    return run(store, "decision.create", {"id": ident, "title": ident, "text": "original", **kwargs})


def test_task_is_report_not_execution(store):
    task(store, status="done")
    run(store, "task.receipt", {"id": "t", "summary": "Assistant says all green", "outcome": "passed", "version": "commit-a"})
    result = store.snapshot("project")["tasks"][0]
    assert result["status"] == "done"
    assert result["verification_state"] == "reported"
    assert not result["verified"]
    for key in ("trusted", "verified", "assurance", "exit_code"):
        with pytest.raises(WorkflowError, match="Unsupported"):
            run(store, "task.receipt", {"id": "t", key: True})
    with pytest.raises(WorkflowError, match="Unknown"):
        run(store, "_task.executed", {"id": "t"})


def test_namespace_isolation_and_source_resolver(tmp_path):
    def resolver(ns, sid):
        return {"id": sid, "role": "tool"} if ns == "a" and sid == "s" else None
    store = WorkflowStore(tmp_path / "s.db", resolver)
    run(store, "task.upsert", {"id": "t", "title": "a", "evidence_ids": ["s"]}, ns="a")
    assert store.snapshot("b")["tasks"] == []
    with pytest.raises(WorkflowError) as err:
        run(store, "task.upsert", {"id": "t", "title": "b", "evidence_ids": ["s"]}, ns="b")
    assert err.value.code == "source_not_found"
    run(store, "task.receipt", {"id": "t", "evidence_ids": ["s"], "outcome": "passed"}, ns="a")
    assert store.snapshot("a")["tasks"][0]["verification_state"] == "reported"


def test_idempotency_conflict_and_revision_lock(store):
    first = run(store, "task.upsert", {"id": "t", "title": "one", "expected_revision": 0}, op="once")
    assert run(store, "task.upsert", {"id": "t", "title": "one", "expected_revision": 0}, op="once") == first
    assert store.snapshot("project")["revision"] == 1
    with pytest.raises(WorkflowError) as err:
        run(store, "task.upsert", {"id": "t", "title": "two"}, op="once")
    assert err.value.code == "operation_conflict"
    with pytest.raises(WorkflowError) as err:
        run(store, "task.upsert", {"id": "t", "title": "two", "expected_revision": 0})
    assert err.value.code == "revision_conflict"
    assert store.snapshot("project")["tasks"][0]["title"] == "one"


def test_concurrent_compare_and_swap_is_atomic(store):
    task(store)
    def update(i):
        try:
            return run(store, "task.upsert", {"id": "t", "title": str(i), "expected_revision": 1}, op=f"race{i}")["ok"]
        except WorkflowError as err:
            return err.code
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(update, range(4)))
    assert result.count(True) == 1
    assert result.count("revision_conflict") == 3


def test_decision_timeline_and_dependency_recheck(store):
    a = decision(store, "a")
    decision(store, "b", depends_on=["a"])
    decision(store, "c", depends_on=["b"])
    changed = run(store, "decision.revise", {"id": "a", "text": "corrected", "reason": "new measurement"})
    assert changed["affected_decisions"] == ["b", "c"]
    now = {x["id"]: x for x in store.snapshot("project")["decisions"]}
    assert [x["text"] for x in now["a"]["history"]] == ["original", "corrected"]
    assert now["b"]["text"] == "original" and now["b"]["needs_review"]
    assert now["c"]["needs_review"]
    old = store.snapshot("project", a["recorded_at"])
    assert [x["id"] for x in old["decisions"]] == ["a"]
    assert old["decisions"][0]["text"] == "original"
    assert len(old["decisions"][0]["history"]) == 1


def test_dependency_cycle_rejected_and_transaction_unchanged(store):
    decision(store, "a")
    decision(store, "b", depends_on=["a"])
    before = store.snapshot("project")["revision"]
    with pytest.raises(WorkflowError) as err:
        run(store, "decision.revise", {"id": "a", "text": "bad", "reason": "cycle", "depends_on": ["b"]})
    assert err.value.code == "dependency_cycle"
    assert store.snapshot("project")["revision"] == before


def test_future_payload_time_is_rejected(store):
    with pytest.raises(WorkflowError, match="Unsupported"):
        task(store, recorded_at="2000-01-01T00:00:00Z")
    assert store.snapshot("project", "2000-01-01T00:00:00Z")["tasks"] == []
    with pytest.raises(WorkflowError, match="timezone"):
        store.snapshot("project", "2025-01-01")


def test_failed_attempt_same_config_only_and_never_blocks(store):
    task(store)
    run(store, "attempt.record", {"id": "a", "task_id": "t", "config": {"seed": 1, "version": "v1", "secret": "not stored"}, "outcome": "failed", "summary": "wrong geometry", "retry_conditions": ["new geometry control"]})
    different = run(store, "reminder.evaluate", {"event": "task_switch", "task_id": "t", "config": {"seed": 2}})
    assert different["reminders"] == []
    same = run(store, "reminder.evaluate", {"event": "task_switch", "task_id": "t", "config": {"version": "v1", "secret": "not stored", "seed": 1}})
    assert len(same["reminders"]) == 1
    assert same["reminders"][0]["blocks_retry"] is False
    assert same["reminders"][0]["retry_conditions"] == ["new geometry control"]
    assert "not stored" not in json.dumps(store.snapshot("project"))
    assert run(store, "reminder.evaluate", {"event": "question", "task_id": "t", "config": {"seed": 2}})["reminders"] == []


def test_reminder_context_cooldown_and_budget(store):
    for i in range(5):
        run(store, "reminder.upsert", {"id": f"r{i}", "title": "Release", "reason": "unverified test", "events": ["question"], "query_terms": ["release"]})
    assert run(store, "reminder.evaluate", {"event": "project_open"})["reminders"] == []
    assert run(store, "reminder.evaluate", {"event": "question", "question": "hello"})["reminders"] == []
    first = run(store, "reminder.evaluate", {"event": "question", "question": "RELEASE now"}, op="evaluate-once")
    assert len(first["reminders"]) == 3
    assert all(r["why_now"].startswith("question:") for r in first["reminders"])
    assert run(store, "reminder.evaluate", {"event": "question", "question": "RELEASE now"}, op="evaluate-once") == first
    second = run(store, "reminder.evaluate", {"event": "question", "question": "release"})
    assert len(second["reminders"]) == 2
    assert run(store, "reminder.evaluate", {"event": "question", "question": "release"})["reminders"] == []
    with pytest.raises(WorkflowError):
        run(store, "reminder.evaluate", {"event": "timer"})


def test_reminder_complete_ignore_and_snooze(store):
    for ident in ("a", "b", "c"):
        run(store, "reminder.upsert", {"id": ident, "title": ident, "reason": "evidence"})
    run(store, "reminder.act", {"id": "a", "disposition": "complete"})
    run(store, "reminder.act", {"id": "b", "disposition": "ignore"})
    run(store, "reminder.act", {"id": "c", "disposition": "snooze", "snooze_seconds": 60})
    assert run(store, "reminder.evaluate", {"event": "project_open"})["reminders"] == []
    future = datetime.now(timezone.utc) + timedelta(seconds=61)
    store._now = lambda: future
    assert [x["id"] for x in run(store, "reminder.evaluate", {"event": "project_open"})["reminders"]] == ["c"]


def test_record_delete_erases_history_operation_cache_and_prevents_replay(store):
    created = task(store, goal="private-text-unique")
    run(store, "attempt.record", {"id": "a", "task_id": "t", "outcome": "failed", "summary": "private-attempt"})
    run(store, "reminder.upsert", {"id": "r", "title": "r", "reason": "private-reminder", "task_id": "t"})
    run(store, "record.delete", {"kind": "task", "id": "t"})
    assert store.snapshot("project", created["recorded_at"])["tasks"] == []
    assert store.snapshot("project")["attempts"] == []
    with store._connect() as db:
        dump = "\n".join(db.iterdump())
    assert "private-text-unique" not in dump
    assert "private-attempt" not in dump
    assert "private-reminder" not in dump
    with pytest.raises(WorkflowError) as err:
        task(store)
    assert err.value.code == "record_deleted"


def test_revoke_old_source_revision_and_dependency_privacy(store):
    first = decision(store, "a", text="private-phrase", evidence_ids=["source1"])
    decision(store, "b", depends_on=["a"], text="derived private-phrase")
    run(store, "decision.revise", {"id": "a", "text": "new unrelated", "reason": "rewrite", "evidence_ids": ["source2"]})
    assert store.revoke_sources("project", ["source1"])["deleted_count"] == 2
    assert store.snapshot("project", first["recorded_at"])["decisions"] == []
    with store._connect() as db:
        assert "private-phrase" not in "\n".join(db.iterdump())
    with pytest.raises(WorkflowError) as err:
        decision(store, "fresh", evidence_ids=["source1"])
    assert err.value.code == "source_revoked"


def test_purge_namespace_does_not_affect_another_namespace(store):
    task(store, evidence_ids=["s"])
    run(store, "task.upsert", {"id": "t", "title": "other"}, ns="other")
    assert store.purge_namespace("project")["deleted_count"] == 1
    assert not store.snapshot("project")["tasks"]
    assert store.snapshot("other")["tasks"][0]["title"] == "other"
    with pytest.raises(WorkflowError) as err:
        task(store, id="new", evidence_ids=["s"])
    assert err.value.code == "source_revoked"


@pytest.mark.parametrize("payload", [{"id": "../x\x00", "title": "a"}, {"id": "a", "title": "x" * 4001}, {"id": "a", "title": "a", "stale_after_seconds": True}, {"id": "a", "title": "a", "evidence_ids": ["s"] * 51}])
def test_input_bounds(store, payload):
    with pytest.raises(WorkflowError):
        run(store, "task.upsert", payload)
    assert store.snapshot("project")["tasks"] == []


def test_missing_evidence_suppresses_reminder(tmp_path):
    available = {"s"}
    store = WorkflowStore(tmp_path / "s.db", lambda ns, sid: {"id": sid} if sid in available else None)
    run(store, "reminder.upsert", {"id": "r", "title": "r", "reason": "why", "evidence_ids": ["s"]})
    available.clear()
    assert run(store, "reminder.evaluate", {"event": "project_open"})["reminders"] == []
    assert not store.snapshot("project")["reminders"][0]["evidence_available"]


def test_decision_delete_erases_historical_derived_body(store):
    decision(store, "a", text="private source")
    decision(store, "b", text="derived private source", depends_on=["a"])
    decision(store, "c", text="third derived private source", depends_on=["b"])
    old = store.snapshot("project")["recorded_at"]
    run(store, "decision.revise", {"id":"b", "text":"new body", "depends_on":[], "reason":"revisit"})
    run(store, "record.delete", {"kind":"decision", "id":"a"})
    assert not store.snapshot("project")["decisions"]
    assert not store.snapshot("project", as_of=old)["decisions"]
    with store._connect() as db:
        assert "private source" not in "\n".join(db.iterdump())


def test_corrected_source_marks_decision_for_review(tmp_path):
    available = {"s": {"id":"s"}}
    store = WorkflowStore(tmp_path / "workflows.db", source_resolver=lambda ns, ident: available.get(ident))
    decision(store, "d", evidence_ids=["s"])
    available.clear()
    view = store.snapshot("project")["decisions"][0]
    assert not view["evidence_available"] and view["needs_review"]
