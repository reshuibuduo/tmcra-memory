from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tmcra_local.api import create_app

from conftest import FakeMemoryModel


def test_loopback_api_auth_and_usage(config_path: Path) -> None:
    app = create_app(config_path, verify_models=False)
    engine = app.state.engine
    engine.graph_factory.retrieval_mode = "heuristic"
    engine.graph_factory.node_model_path = ""
    engine.graph_factory.path_model_path = ""
    engine.graph_factory.graph_environment.update(
        {
            "TMCRA_WRITE_EMBEDDER_INDEX_MODE": "off",
            "TMCRA_EMBEDDER_INDEX_RECALL_MODE": "off",
            "TMCRA_EMBEDDER_PRE_RECALL_MODE": "off",
            "TMCRA_EMBEDDER_FUSION_MODE": "off",
        }
    )
    fake = FakeMemoryModel(engine._record_usage)
    engine.llm = fake
    engine.writer.flash_client = fake
    engine.writer.pro_client = fake
    token = Path(app.state.local_token_path).read_text(encoding="utf-8").strip()
    auth = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        health = client.get("/v1/health")
        assert health.status_code == 200
        assert "test-only-key-never-sent" not in health.text
        assert health.json()["contains_production_control_plane"] is False
        for excluded_route in (
            "/v1/accounts",
            "/v1/billing",
            "/v1/subscriptions",
            "/v1/staff",
            "/v1/tenants",
        ):
            assert client.get(excluded_route, headers=auth).status_code == 404
        assert client.get("/v1/projects").status_code == 401
        written = client.post(
            "/v1/messages",
            headers=auth,
            json={
                "project_id": "api-project",
                "session_id": "api-session",
                "role": "user",
                "actor": {"origin_kind": "human_authored", "capture_method": "explicit_api_declaration", "provenance_strength": "caller_declared"},
                "content": "API 本地链路测试",
                "source_app": "test-client",
                "native_message_id": "api-message-1",
            },
        )
        assert written.status_code == 200, written.text
        recalled = client.post(
            "/v1/recall",
            headers=auth,
            json={"project_id": "api-project", "query": "本地链路"},
        )
        assert recalled.status_code == 200, recalled.text
        assert recalled.json()["evidence_windows"]
        messages = client.get(
            "/v1/messages?project_id=api-project", headers=auth
        )
        assert messages.status_code == 200
        assert messages.json()["messages"][0]["role"] == "user"
        usage = client.get("/v1/usage", headers=auth)
        assert usage.status_code == 200
        assert usage.json()["totals"]["calls"] >= 1
        deleted = client.delete("/v1/projects/api-project", headers=auth)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_messages"] == 1
        assert client.get("/v1/projects", headers=auth).json()["projects"] == []
