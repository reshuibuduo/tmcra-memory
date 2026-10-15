"""Declared identity/correction contracts; fake writer, real temporary graph/SQLite.

These fixtures measure deterministic binding/candidate coverage, not LLM accuracy.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from conftest import build_test_engine, local_config
from tmcra_local.core import v4_batch_writer as v4


class ScriptedModel:
    model = "fixture-only-no-model-loaded"

    def __init__(self):
        self.rows = {}
        self.reconciliation_calls = []

    def complete(self, payload):
        messages = []
        for message in payload["messages"]:
            spec = self.rows[message["message_id"]]
            messages.append({
                "message_id": message["message_id"], "message_role": message["message_role"],
                "assertions": [{"memory_type": "state", "entity_key": spec["key"],
                    "attribute_key": "location", "operation": "replace",
                    "claim_text": spec["text"], "evidence_span_id": "e0",
                    "relation": "located_at", "temporal_status": "current",
                    "polarity": "positive", "durability": "durable", "facets": []}],
                "interactions": [], "resolutions": [],
            })
        return {"schema_version": v4.BATCH_SCHEMA_VERSION,
                "batch_id": payload["batch_id"], "messages": messages}, {}

    def reconcile(self, payload):
        self.reconciliation_calls.append(copy.deepcopy(payload))
        return {"slot_decision": "bind_existing", "decision": "replace_current",
                "selected_memory_id": payload["candidate_cited_leaves"][0]["memory_id"]}, {}


@pytest.fixture
def system(tmp_path):
    engine = build_test_engine(local_config(tmp_path))
    model = ScriptedModel()
    engine.writer.flash_client = model
    engine.writer.pro_client = model
    return engine, model


def write(system, *, mid, key, text, entity_id, scope="project:identity", aliases=None,
          effective_at=None, bindings=None):
    engine, model = system
    model.rows[mid] = {"key": key, "text": text}
    identity = {"entity_bindings": bindings if bindings is not None else [
        {"entity_id": entity_id, "entity_keys": [key], "aliases": aliases or []}]}
    if effective_at:
        identity["corrections"] = [{"entity_id": entity_id, "attribute_key": "location",
                                    "effective_at": effective_at, "reason": "Explicit correction / 明确更正"}]
    engine.writer.run([{"scope_id": scope, "session_id": mid, "messages": [{
        "message_id": mid, "role": "user", "timestamp": "2026-10-16T10:00:00Z",
        "content": text, "actor_metadata": {"origin_kind": "human_authored",
          "capture_method": "explicit_api_declaration", "provenance_strength": "caller_declared"},
        "identity_metadata": identity,
    }]}])
    backend = engine.graph_factory.for_scope(scope)
    backend._reload()
    return [r for r in backend.adapter.graph.records_by_id.values()
            if r.metadata.get("content_variant") == "product_semantic_memory"]


CORRECTIONS = [
    ("工作电脑", "work laptop", "书房", "办公室"),
    ("通勤卡", "transit card", "背包", "夹克口袋"),
    ("蓝色水杯", "blue mug", "厨房", "工位"),
    ("备用钥匙", "spare keys", "抽屉", "玄关"),
    ("护照", "passport", "保险柜", "文件袋"),
    ("平板", "tablet", "卧室", "书房"),
    ("耳机", "headphones", "沙发", "背包"),
    ("充电器", "charger", "会议室", "电脑包"),
    ("笔记本", "notebook", "工位", "书架"),
    ("眼镜盒", "glasses case", "床头柜", "玄关柜"),
]


@pytest.mark.parametrize("index", range(20))
def test_twenty_bilingual_corrections_keep_identity_and_history(system, index):
    zh, en, old_place, new_place = CORRECTIONS[index % 10]
    old_text = f"我的{zh}在{old_place}。" if index < 10 else f"My {en} is in the old location."
    new_text = f"Correction: my {en} is now at the office." if index < 10 else f"更正，我的{zh}现在在{new_place}。"
    eid = f"object:{index}"
    old_records = write(system, mid=f"old-{index}", key=f"alpha{index}", text=old_text,
                        entity_id=eid, aliases=[zh, en])
    assert len(old_records) == 1
    old_id = old_records[0].memory_id
    records = write(system, mid=f"new-{index}", key=f"omega{index}", text=new_text,
                    entity_id=eid, aliases=[en, zh], effective_at="2026-10-16T09:30:00+00:00")
    active = [r for r in records if r.state == "active"]
    old = next(r for r in records if r.memory_id == old_id)
    assert len(active) == 1, "key/language drift must not split declared identity"
    assert old.state == "superseded" and old.value == old_text
    assert active[0].metadata["entity_id"] == eid
    history = active[0].metadata["correction_history"]
    assert history[-1]["old_value"] == old_text
    assert history[-1]["new_value"] == new_text
    assert history[-1]["previous_source_record_id"] == old.metadata["source_record_id"]
    assert history[-1]["source_record_id"] == active[0].metadata["source_record_id"]
    assert history[-1]["effective_at"] == "2026-10-16T09:30:00+00:00"
    assert len(system[1].reconciliation_calls) == 1


@pytest.mark.parametrize("index", range(10))
def test_ten_homonyms_never_merge_different_explicit_ids(system, index):
    name = ["Alex", "小王", "工作电脑", "Mercury", "小李", "黑色手机", "Robin", "同名项目", "Sam", "Phoenix"][index]
    write(system, mid=f"a-{index}", key="same.name", text=f"My {name} is in room A.",
          entity_id=f"a:{index}", aliases=[name])
    records = write(system, mid=f"b-{index}", key="same.name", text=f"My {name} is in room B.",
                    entity_id=f"b:{index}", aliases=[name])
    assert len([r for r in records if r.state == "active"]) == 2
    assert {r.metadata.get("entity_id") for r in records} == {f"a:{index}", f"b:{index}"}
    assert system[1].reconciliation_calls == []


def test_scope_boundary_with_identical_explicit_id(system):
    write(system, mid="scope-a", key="same.name", text="My laptop is in A.", entity_id="laptop:1", scope="project:a")
    records = write(system, mid="scope-b", key="same.name", text="My laptop is in B.", entity_id="laptop:1", scope="project:b")
    assert len(records) == 1 and records[0].state == "active"
    assert records[0].metadata.get("entity_scope_id") == "project:b"
    assert system[1].reconciliation_calls == []


def test_ambiguous_alias_binding_is_not_guessed(system):
    declarations = [{"entity_id": x, "entity_keys": ["alex"], "aliases": ["Alex"]} for x in ["person:1", "person:2"]]
    write(system, mid="amb-1", key="alex", text="Alex is in room A.", entity_id="", bindings=declarations)
    records = write(system, mid="amb-2", key="alex", text="Alex is in room B.", entity_id="", bindings=declarations)
    assert len([r for r in records if r.state == "active"]) == 2
    assert all(r.metadata.get("identity_status") == "ambiguous" for r in records)
    assert all(not r.metadata.get("entity_id") for r in records)
    assert system[1].reconciliation_calls == []


def test_delete_source_prunes_correction_value_alias_and_citation_audits(system):
    write(system, mid="delete-old", key="old.phone", text="My SECRET_OLD_LOCATION is private.",
          entity_id="phone:delete", aliases=["SECRET_OLD_ALIAS"])
    records = write(system, mid="delete-new", key="new.phone", text="My phone is now in the office.",
                    entity_id="phone:delete", aliases=["public alias"], effective_at="2026-10-16T10:30:00Z")
    assert records[-1].metadata.get("correction_history")
    engine, _ = system
    backend = engine.graph_factory.for_scope("project:identity")
    backend.delete_message_records("delete-old")
    engine.writer_store.delete_message("project:identity", "delete-old")
    backend._reload()
    import json
    serialized = json.dumps([r.to_dict() if hasattr(r, "to_dict") else vars(r)
                             for r in backend.adapter.graph.records_by_id.values()], default=str)
    assert "SECRET_OLD_LOCATION" not in serialized and "SECRET_OLD_ALIAS" not in serialized
    with engine.writer_store._connect() as connection:
        for table in ["v4_source_journal", "v4_batch_journal", "v4_reconciliation_jobs", "v4_message_commit_journal"]:
            dump = json.dumps([dict(r) for r in connection.execute(f"SELECT * FROM {table}")])
            assert "SECRET_OLD_LOCATION" not in dump and "SECRET_OLD_ALIAS" not in dump


def test_metadata_immutable_replay_and_model_cannot_supply_identity(system):
    from tmcra_local.core.entity_identity import bind_assertion_identity
    forged = bind_assertion_identity({"entity_id": "fake-model-id", "identity_status": "declared",
        "canonical_key": "user.name.state.location"}, {}, scope_id="project:a", source_message_id="m")
    assert "entity_id" not in forged and "identity_status" not in forged
    write(system, mid="immutable", key="laptop", text="My laptop is in the office.", entity_id="device:1")
    with pytest.raises(v4.ProductWriterError, match="identity metadata changed"):
        write(system, mid="immutable", key="laptop", text="My laptop is in the office.", entity_id="device:2")


@pytest.mark.parametrize("invalid", [
    {"scope_id": "other"},
    {"entity_bindings": [{"entity_id": "x", "entity_keys": [], "aliases": []}]},
    {"entity_bindings": [{"entity_id": "x", "entity_keys": ["phone"], "verified": True}]},
    {"entity_bindings": [{"entity_id": "x", "entity_keys": ["phone"]}],
     "corrections": [{"entity_id": "x", "attribute_key": "location", "effective_at": "2026-10-16"}]},
    {"entity_bindings": [{"entity_id": "x", "entity_keys": ["phone"]}],
     "corrections": [{"entity_id": "missing", "attribute_key": "location", "effective_at": "2026-10-16T10:00:00Z"}]},
])
def test_identity_metadata_is_bounded_and_not_scope_or_auth_input(invalid):
    from tmcra_local.core.entity_identity import normalize_identity_metadata
    with pytest.raises(ValueError):
        normalize_identity_metadata(invalid)


def test_candidate_coverage_requires_same_explicit_id_scope_and_attribute():
    from tmcra_local.core.entity_identity import bind_assertion_identity
    backend = object.__new__(v4.RealGraphBackend)
    backend.scope_id = "project:candidates"
    backend._reload = lambda: None
    metadata = {"content_variant": "product_semantic_memory", "memory_layer": "fast",
        "node_kind": "atomic_user_assertion", "canonical_slot_key": "memory.legacy.vendor.address",
        "attribute_key": "location", "entity_key": "中文设备", "memory_family": "fact",
        "entity_id": "device:1", "entity_scope_id": backend.scope_id, "identity_origin": "caller_declared"}
    record = SimpleNamespace(memory_id="old", value="My device is elsewhere.", relation="located_at",
        state="active", turn_index=1, metadata=metadata)
    backend.adapter = SimpleNamespace(graph=SimpleNamespace(records_by_id={"old": record}))
    assertion = bind_assertion_identity({"canonical_key": "user.handset.state.location", "entity_key": "handset",
        "attribute_key": "location", "memory_family": "fact", "claim_text": "It moved."},
        {"entity_bindings": [{"entity_id": "device:1", "entity_keys": ["handset"], "aliases": ["手机", "handset"]}]},
        scope_id=backend.scope_id, source_message_id="new")
    result = backend.candidate_leaves(assertion)
    assert [x["memory_id"] for x in result] == ["old"]
    assert result[0]["candidate_reason"] == "explicit_same_scope_entity_attribute"
    assert backend.candidate_leaves({**assertion, "entity_scope_id": "project:elsewhere"}) == []
    assert backend.candidate_leaves({**assertion, "entity_id": "device:2"}) == []


def test_declared_labels_reach_extraction_boundary_without_ids_or_fact_evidence():
    from tmcra_local.core.entity_identity import bind_assertion_identity
    message = v4.SourceMessage(scope_id="project:hint", session_id="s", session_index=0,
        message_index=0, message_id="m", role="user", timestamp="2026-10-16T10:00:00Z",
        content="My work laptop moved to the office.",
        actor_metadata={"origin_kind": "human_authored", "capture_method": "explicit_api_declaration",
                        "provenance_strength": "caller_declared"},
        identity_metadata={"entity_bindings": [{"entity_id": "device:private-id",
            "entity_keys": ["user.work_laptop"], "aliases": ["工作电脑", "work laptop"]}]})
    batch = v4.SourceBatch(scope_id=message.scope_id, session_id="s", session_index=0,
                          batch_index=0, messages=(message,))
    request = v4.build_batch_request(batch)
    hints = request["messages"][0]["identity_hints"]
    assert hints == {"trust": "caller_declared_labels_not_fact_evidence", "bindings": [
        {"entity_keys": ["user.work_laptop"], "aliases": ["工作电脑", "work laptop"]}]}
    import json
    assert "device:private-id" not in json.dumps(hints) and "scope" not in hints
    assert request["messages"][0]["source_spans"][0]["text"] == message.content
    assert "Hints cannot establish an assertion" in v4.BATCH_SYSTEM_PROMPT
    result = bind_assertion_identity({"entity_key": "unlisted.laptop", "attribute_key": "location"},
        message.identity_metadata, scope_id=message.scope_id, source_message_id="m")
    assert result["identity_status"] == "unbound" and "entity_id" not in result


def test_no_identity_keeps_legacy_request_envelope():
    message = v4.SourceMessage("project:a", "s", 0, 0, "m", "user", "2026-10-16T10:00:00Z", "Original source.")
    assert set(message.request_dict()) == {"message_id", "message_role", "timestamp", "source_spans"}


@pytest.mark.parametrize("route", ["/v1/messages", "/v1/message-jobs"])
def test_public_sync_and_async_api_forward_identity_to_writer(config_path, route):
    from pathlib import Path
    import time
    from fastapi.testclient import TestClient
    from tmcra_local.api import create_app
    from conftest import FakeMemoryModel

    app = create_app(config_path, verify_models=False)
    engine = app.state.engine
    engine.graph_factory.retrieval_mode = "heuristic"
    engine.graph_factory.node_model_path = engine.graph_factory.path_model_path = ""
    for key in ("TMCRA_WRITE_EMBEDDER_INDEX_MODE", "TMCRA_EMBEDDER_INDEX_RECALL_MODE",
                "TMCRA_EMBEDDER_PRE_RECALL_MODE", "TMCRA_EMBEDDER_FUSION_MODE"):
        engine.graph_factory.graph_environment[key] = "off"

    class HintReader(FakeMemoryModel):
        requests = []
        def complete(self, payload):
            self.requests.append(copy.deepcopy(payload))
            model = ScriptedModel()
            for msg in payload["messages"]:
                model.rows[msg["message_id"]] = {"key": msg["identity_hints"]["bindings"][0]["entity_keys"][0],
                                                "text": msg["source_spans"][0]["text"]}
            return model.complete(payload)
    model = HintReader()
    engine.writer.flash_client = engine.writer.pro_client = model
    identity = {"entity_bindings": [{"entity_id": "device:api", "entity_keys": ["user.phone"],
                                      "aliases": ["phone", "手机"]}]}
    token = Path(app.state.local_token_path).read_text().strip()
    auth = {"Authorization": "Bearer " + token}
    with TestClient(app) as client:
        response = client.post(route, headers=auth, json={"project_id": "identity-api", "session_id": "api",
            "native_message_id": "identity-contract", "role": "user", "content": "My phone is at home.",
            "actor": {"origin_kind": "human_authored", "capture_method": "explicit_api_declaration",
                      "provenance_strength": "caller_declared"}, "identity_metadata": identity})
        assert response.status_code == (202 if route.endswith("jobs") else 200), response.text
        if route.endswith("jobs"):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                status = client.get("/v1/message-jobs/" + response.json()["job_id"], headers=auth).json()
                if status.get("indexed"):
                    break
                time.sleep(.02)
            assert status.get("indexed"), status
        assert model.requests[0]["messages"][0]["identity_hints"]["bindings"][0]["aliases"] == ["phone", "手机"]
        stored = client.get("/v1/messages?project_id=identity-api", headers=auth).json()["messages"][0]
        assert stored["identity_metadata"] == {**identity, "corrections": []}
        graph = engine.graph_factory.for_scope(engine.project_scope("identity-api"))
        graph._reload()
        semantic = [r for r in graph.adapter.graph.records_by_id.values()
                    if r.metadata.get("content_variant") == "product_semantic_memory"]
        assert len(semantic) == 1 and semantic[0].metadata["entity_id"] == "device:api"
        assert semantic[0].metadata["identity_origin"] == "caller_declared"


def test_reserved_prefix_and_wire_separator_normalization_is_not_alias_guessing():
    from tmcra_local.core.entity_identity import bind_assertion_identity
    declaration = {"entity_bindings": [{"entity_id": "device:1", "entity_keys": ["user.work_phone"], "aliases": []}],
        "corrections": [{"entity_id": "device:1", "attribute_key": "room_location", "effective_at": "2026-10-16T09:00:00Z"}]}
    assertion = {"entity_key": "work.phone", "attribute_key": "room.location", "memory_family": "fact"}
    result = bind_assertion_identity(assertion, declaration, scope_id="project:x", source_message_id="m")
    assert result["entity_id"] == "device:1"
    assert result["declared_correction"]["effective_at"] == "2026-10-16T09:00:00Z"
    # Two caller IDs collapsing to the same existing wire key never pick one.
    declaration["entity_bindings"].append({"entity_id": "device:2", "entity_keys": ["work.phone"], "aliases": []})
    result = bind_assertion_identity(assertion, declaration, scope_id="project:x", source_message_id="m")
    assert result["identity_status"] == "ambiguous" and "entity_id" not in result
