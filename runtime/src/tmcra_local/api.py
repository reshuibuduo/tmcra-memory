from __future__ import annotations

import hmac
import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from pydantic import BaseModel, Field, ConfigDict

from .auth import ensure_local_token
from .core import ProductWriterError
from .engine import LocalEngineError, LocalMemoryEngine


LOGGER = logging.getLogger("tmcra_local.api")


class WorkflowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1, max_length=64)
    operation_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class MessageIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=512)
    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(min_length=1, max_length=1_000_000)
    source_app: str = Field(default="manual", min_length=1, max_length=80)
    native_thread_id: str = Field(default="", max_length=512)
    native_message_id: str = Field(default="", max_length=512)
    project_title: str = Field(default="", max_length=200)
    session_title: str = Field(default="", max_length=200)
    occurred_at: str = Field(default="", max_length=80)
    actor: dict[str, Any] = Field(default_factory=dict)
    identity_metadata: dict[str, Any] = Field(default_factory=dict)
    visibility: Literal["project", "global", "both"] = "project"
    message_id: str = Field(default="", max_length=512)


class RecallIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1, max_length=100_000)
    top_k: int = Field(default=8, ge=1, le=32)


def create_app(
    config_path: str | Path,
    *,
    verify_models: bool = True,
    runtime_mode_override=None,
) -> FastAPI:
    engine = LocalMemoryEngine(config_path, verify_models=verify_models, runtime_mode_override=runtime_mode_override)
    installation = engine.config.get("installation")
    if not isinstance(installation, Mapping):
        raise RuntimeError("local runtime installation config is missing")
    token_file, token, _ = ensure_local_token(str(installation.get("config_root") or ""))

    @asynccontextmanager
    async def lifespan(_app):
        engine.start_worker()
        try:
            yield
        finally:
            engine.close()

    app = FastAPI(
        lifespan=lifespan,
        title="TMCRA Local Memory API",
        version="0.1.0",
        description=(
            "Loopback-only owner-local API. It has no TMCRA cloud account, "
            "subscription, billing, staff, or tenant endpoints."
        ),
    )
    app.state.engine = engine
    app.state.local_token_path = str(token_file)

    def authorize(authorization: str = Header(default="")) -> None:
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="invalid local bearer token")

    @app.exception_handler(Exception)
    async def local_error_handler(_, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        if isinstance(exc, (LocalEngineError, ProductWriterError, ValueError)):
            return JSONResponse(
                status_code=400,
                content={"error": exc.__class__.__name__, "detail": str(exc)},
            )
        LOGGER.exception("unhandled local API failure", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": "InternalServerError", "detail": "local runtime failure"},
        )

    from .runtime_modes import RuntimeModeError
    @app.exception_handler(RuntimeModeError)
    async def mode_error(_, exc):
        return JSONResponse(status_code=403, content={'error': 'runtime_mode_disabled', 'runtime_mode': exc.mode, 'detail': str(exc)})

    from .workflows import WorkflowError
    from .memory_bridge import BridgeError

    @app.exception_handler(WorkflowError)
    async def workflow_error(_, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": str(exc)})

    @app.exception_handler(BridgeError)
    async def bridge_error(_, exc):
        return JSONResponse(status_code=409, content={"error": "bridge_rejected", "detail": str(exc)})

    @app.get("/workflows")
    def workflow_redirect():
        return RedirectResponse("/workflows/workflow-center.html")

    @app.get("/workflows/{asset}")
    def workflow_ui(asset: str):
        if asset not in {"workflow-center.html", "workflow-center.js", "workflow-center.css"}:
            raise HTTPException(status_code=404)
        return FileResponse(Path(__file__).parent / "ui" / asset, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})

    @app.get("/v1/projects/{project_id}/workflows", dependencies=[Depends(authorize)])
    def workflow_snapshot(project_id: str, as_of: str | None = Query(default=None, max_length=80)):
        return engine.workflows.snapshot(project_id, as_of=as_of)

    @app.post("/v1/projects/{project_id}/workflows", dependencies=[Depends(authorize)])
    def workflow_action(project_id: str, body: WorkflowIn):
        engine.require_enrich('workflow action')
        return engine.workflows.execute(project_id, body.action, body.payload, body.operation_id)

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return engine.health()

    @app.get("/v1/projects", dependencies=[Depends(authorize)])
    def projects() -> dict[str, Any]:
        return {"projects": engine.store.projects()}

    @app.get("/v1/sessions", dependencies=[Depends(authorize)])
    def sessions(project_id: str = Query(default="")) -> dict[str, Any]:
        return {"sessions": engine.store.sessions(project_id or None)}

    @app.get("/v1/messages", dependencies=[Depends(authorize)])
    def messages(
        project_id: str = Query(default=""),
        session_id: str = Query(default=""),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        return {
            "messages": engine.store.messages(
                project_id=project_id,
                session_id=session_id,
                limit=limit,
            )
        }

    @app.post("/v1/messages", dependencies=[Depends(authorize)])
    def ingest(payload: MessageIn) -> dict[str, Any]:
        values = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        if not values["native_thread_id"]:
            values["native_thread_id"] = values["session_id"]
        return engine.ingest_message(**values)

    @app.post("/v1/message-jobs", dependencies=[Depends(authorize)], status_code=202)
    def enqueue(payload: MessageIn):
        values = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
        if not values['native_thread_id']:
            values['native_thread_id'] = values['session_id']
        return engine.enqueue_message(**values)

    @app.get("/v1/message-jobs/{job_id}", dependencies=[Depends(authorize)])
    def job(job_id: str):
        result = engine.job_status(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail='job does not exist')
        return result

    class RetryIn(BaseModel):
        model_config = ConfigDict(extra='forbid')
        confirmed: bool = False

    @app.post("/v1/message-jobs/{job_id}/retry", dependencies=[Depends(authorize)], status_code=202)
    def retry(job_id: str, payload: RetryIn):
        return engine.retry_job(job_id, confirmed=payload.confirmed)

    @app.delete("/v1/messages/{message_id}", dependencies=[Depends(authorize)])
    def delete_message(message_id: str) -> dict[str, Any]:
        return engine.delete_message(message_id)

    @app.delete("/v1/projects/{project_id}", dependencies=[Depends(authorize)])
    def delete_project(project_id: str) -> dict[str, Any]:
        return engine.delete_project(project_id)

    @app.post("/v1/recall", dependencies=[Depends(authorize)])
    def recall(payload: RecallIn) -> dict[str, Any]:
        return engine.recall(
            project_id=payload.project_id, query=payload.query, top_k=payload.top_k
        )

    @app.get("/v1/projects/{project_id}/graph", dependencies=[Depends(authorize)])
    def graph(project_id: str) -> dict[str, Any]:
        return engine.visual_atlas(project_id=project_id)

    @app.post(
        "/v1/projects/{project_id}/knowledge/build",
        dependencies=[Depends(authorize)],
    )
    def build_knowledge(project_id: str) -> dict[str, Any]:
        return engine.build_knowledge(project_id=project_id)

    @app.get("/v1/projects/{project_id}/knowledge", dependencies=[Depends(authorize)])
    def knowledge(project_id: str) -> dict[str, Any]:
        result = engine.knowledge(project_id=project_id)
        if result is None:
            raise HTTPException(status_code=404, detail="knowledge projection does not exist")
        return result

    @app.get("/v1/usage", dependencies=[Depends(authorize)])
    def usage(
        project_id: str = Query(default=""),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        return engine.usage(project_id=project_id, limit=limit)

    return app


__all__ = ["MessageIn", "RecallIn", "create_app"]
