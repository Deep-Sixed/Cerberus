"""Cerberus API — thin FastAPI surface over the policy core.

Session 2 shape: server-token auth (identity records take over in S4),
OpenAI-compatible ingress, one dispatch loop for every alias mode.
"""

from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from cerberus.registry import CerberusConfig, ConfigDocument, load_config_document
from cerberus.router.dispatch import dispatch
from cerberus.state import InMemoryCooldownStore
from cerberus.telemetry import TelemetryEmitter


def _document_for(config: CerberusConfig) -> ConfigDocument:
    return ConfigDocument(
        config=config,
        version=config.metadata.version,
        checksum="sha256:in-memory",
        source_path="<in-memory>",
    )


def create_app(
    config: CerberusConfig | ConfigDocument | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    telemetry_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    if config is None:
        document = load_config_document()
    elif isinstance(config, ConfigDocument):
        document = config
    else:
        document = _document_for(config)

    store = InMemoryCooldownStore()
    telemetry = TelemetryEmitter(document.config.telemetry, telemetry_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient(transport=http_transport, timeout=300.0)
        await telemetry.start()
        try:
            yield
        finally:
            await telemetry.close()
            await app.state.http_client.aclose()

    app = FastAPI(title="Cerberus", version="0.1.0", lifespan=lifespan)
    app.state.document = document
    app.state.cooldowns = store

    def authenticated(request: Request) -> bool:
        token_env = document.config.server.api_token_env
        if token_env is None:
            return True
        expected = os.environ.get(token_env, "")
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            return bool(expected) and secrets.compare_digest(supplied, expected)
        except TypeError:
            # non-ASCII header input is a bad credential, never a server error
            return False

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "cerberus",
            "config_version": document.version,
            "config_checksum": document.checksum,
            "cooldowns": store.snapshot(),
        }

    @app.get("/v1/models", response_model=None)
    async def models(request: Request) -> dict[str, Any] | JSONResponse:
        if not authenticated(request):
            return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
        return {
            "object": "list",
            "data": [
                {"id": alias_name, "object": "model", "owned_by": "cerberus", "cerberus_mode": alias.mode}
                for alias_name, alias in document.config.aliases.items()
            ],
        }

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
        if not authenticated(request):
            return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
        try:
            body = await request.json()
        except json.JSONDecodeError, UnicodeDecodeError:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "JSON object body required"}})
        alias_name = body.get("model")
        if not isinstance(alias_name, str) or alias_name not in document.config.aliases:
            return JSONResponse(
                status_code=404,
                content={"error": {"message": f"Unknown alias {alias_name!r}; see /v1/models"}},
            )
        return await dispatch(
            body=body,
            alias_name=alias_name,
            document=document,
            store=store,
            client=request.app.state.http_client,
            telemetry=telemetry,
        )

    return app
