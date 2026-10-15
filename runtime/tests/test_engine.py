from __future__ import annotations

from pathlib import Path

from tmcra_local.engine import LocalMemoryEngine

from conftest import build_test_engine, local_config


def _ingest(
    engine: LocalMemoryEngine,
    *,
    project: str,
    session: str,
    role: str,
    content: str,
    app: str,
    native_id: str,
    visibility: str = "project",
):
    return engine.ingest_message(
        project_id=project,
        project_title="North Star",
        session_id=session,
        session_title=session,
        role=role,
        content=content,
        source_app=app,
        native_thread_id=session,
        native_message_id=native_id,
        actor={"actor_role": role, "actor_id": "owner" if role == "user" else "agent",
               "origin_kind": "human_authored" if role == "user" else "assistant_generated",
               "capture_method": "explicit_api_declaration", "provenance_strength": "caller_declared"},
        visibility=visibility,
    )


def test_cross_app_cross_session_recall_actor_separation_and_delete(
    engine: LocalMemoryEngine,
) -> None:
    user = _ingest(
        engine,
        project="project-a",
        session="codex-session",
        role="user",
        content="项目代号是北斗，API 客户端仍需增加重试支持。",
        app="codex",
        native_id="codex-user-1",
        visibility="both",
    )
    assistant = _ingest(
        engine,
        project="project-a",
        session="harness-session",
        role="assistant",
        content="已在 DeepSeek Harness 侧完成 API 客户端重试与幂等处理。",
        app="deepseek-harness",
        native_id="harness-assistant-1",
    )
    assert user["actor"]["actor_role"] == "user"
    assert assistant["actor"]["actor_role"] == "assistant"

    recalled = engine.recall(project_id="project-a", query="北斗 API 重试", top_k=8)
    assert recalled["resolved_scopes"] == ["global:owner", "project:project-a"]
    texts = [window["text"] for window in recalled["evidence_windows"]]
    assert any("北斗" in text for text in texts)
    assert any("Harness" in text for text in texts)
    roles = {window["actor_role"] for window in recalled["evidence_windows"]}
    assert {"user", "assistant"}.issubset(roles)

    other_project = engine.recall(
        project_id="project-b", query="项目代号是什么", top_k=8
    )
    assert any(
        "北斗" in window["text"] for window in other_project["evidence_windows"]
    )
    assert all(
        window["scope_id"] == "global:owner"
        for window in other_project["evidence_windows"]
    )

    atlas = engine.visual_atlas(project_id="project-a")
    assert atlas["scope_name"] == "project:project-a"
    assert atlas["counts"]["sessions"] == 2
    knowledge = engine.build_knowledge(project_id="project-a")
    assert knowledge
    assert engine.knowledge(project_id="project-a") is not None
    usage = engine.usage(project_id="project-a")
    assert usage["tmcra_charge"] == 0
    assert usage["totals"]["calls"] >= 2

    deletion = engine.delete_message(user["message_id"])
    assert deletion["deleted"] is True
    assert deletion["sqlite_free_pages_rewritten"] is True
    assert deletion["external_backup_copies_removed"] is False
    after = engine.recall(project_id="project-a", query="北斗", top_k=8)
    assert all("北斗" not in window["text"] for window in after["evidence_windows"])
    assert engine.knowledge(project_id="project-a") is None


def test_failed_message_can_retry_with_same_identity(engine: LocalMemoryEngine) -> None:
    original = engine.writer.flash_client

    class Failing:
        model = "failing-test-model"

        @staticmethod
        def complete(_):
            raise RuntimeError("temporary provider failure")

    engine.writer.flash_client = Failing()
    kwargs = dict(
        project="project-r",
        session="session-r",
        role="user",
        content="请记住重试测试。",
        app="codex",
        native_id="retry-1",
    )
    try:
        _ingest(engine, **kwargs)
    except RuntimeError as exc:
        assert "temporary provider failure" in str(exc)
    else:
        raise AssertionError("the first write should fail")
    engine.writer.flash_client = original
    retried = _ingest(engine, **kwargs)
    assert retried["created"] is False
    assert retried["scopes"][0]["status"] == "committed"


def test_enabled_personal_knowledge_uses_grounded_model_projection(
    tmp_path: Path,
) -> None:
    engine = build_test_engine(
        local_config(tmp_path, knowledge_enabled=True)
    )
    _ingest(
        engine,
        project="knowledge-project",
        session="knowledge-session",
        role="user",
        content="项目决定采用本地优先的记忆部署方式。",
        app="codex",
        native_id="knowledge-user-1",
    )
    knowledge = engine.build_knowledge(project_id="knowledge-project")
    assert knowledge["projection_state"] == "ready"
    assert knowledge["generated_by"] == "local-personal-knowledge-agent"
    assert knowledge["pages"]
    assert knowledge["evidence_catalog"]
    assert all(page["evidence_ids"] for page in knowledge["pages"])
    stages = {
        item["task"]
        for item in engine.usage(project_id="knowledge-project")["recent"]
    }
    assert "personal_knowledge" in stages


def test_project_delete_removes_project_and_global_memory(engine: LocalMemoryEngine) -> None:
    written = _ingest(
        engine,
        project="project-delete",
        session="session-delete",
        role="user",
        content="待删除项目的唯一标记是星河回收。",
        app="codex",
        native_id="delete-project-1",
        visibility="both",
    )
    assert written["scopes"]
    assert engine.store.messages(project_id="project-delete")
    result = engine.delete_project("project-delete")
    assert result["deleted"] is True
    assert result["deleted_messages"] == 1
    assert engine.store.messages(project_id="project-delete") == []
    assert all(
        item["project_id"] != "project-delete" for item in engine.store.projects()
    )
    recalled = engine.recall(
        project_id="another-project", query="星河回收", top_k=8
    )
    assert all(
        "星河回收" not in window["text"]
        for window in recalled["evidence_windows"]
    )
