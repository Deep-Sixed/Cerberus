"""Cerberus API — thin FastAPI surface over the policy core.

Policy (providers, aliases, identities) hot-swaps through the config lifecycle;
infrastructure bindings (server, telemetry sink, state backend, authentik
issuer) are fixed at boot and change via restart — deliberately, so a config
activation can never silently rebind the process's trust anchors.
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
from pydantic import ValidationError

from cerberus.control import ConfigLifecycle
from cerberus.identity import (
    AuthentikVerifier,
    IdentityContext,
    authorization_error,
    identity_for_client_id,
    resolve_identity,
    supplied_credential,
)
from cerberus.registry import CerberusConfig, ConfigDocument, load_config_document
from cerberus.router.dispatch import dispatch, shadow_decision_event, unauthorized_event
from cerberus.state import InMemoryCooldownStore, SqliteCooldownStore
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
    jwks_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    if config is None:
        document = load_config_document()
    elif isinstance(config, ConfigDocument):
        document = config
    else:
        document = _document_for(config)

    lifecycle = ConfigLifecycle(document)
    boot_config = document.config  # infrastructure bindings: fixed at boot
    state_path = boot_config.state.path
    store = SqliteCooldownStore(state_path) if state_path else InMemoryCooldownStore()
    telemetry = TelemetryEmitter(boot_config.telemetry, telemetry_transport)
    verifier = (
        AuthentikVerifier(boot_config.authentik, jwks_transport) if boot_config.authentik is not None else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient(transport=http_transport, timeout=300.0)
        await telemetry.start()
        try:
            yield
        finally:
            await telemetry.close()
            await app.state.http_client.aclose()
            if verifier is not None:
                await verifier.aclose()

    app = FastAPI(title="Cerberus", version="0.1.0", lifespan=lifespan)
    app.state.lifecycle = lifecycle
    app.state.cooldowns = store

    async def resolve(request: Request, config: CerberusConfig) -> IdentityContext | None:
        static = resolve_identity(config, request)
        if static is not None:
            return static
        if verifier is None:
            return None
        token = supplied_credential(request)
        # a JWT has two dots; static cb- keys never do
        if not token or token.count(".") != 2:
            return None
        client_id = await verifier.verify(token)
        if client_id is None:
            return None
        return identity_for_client_id(config, client_id)

    def authenticated(request: Request) -> bool:
        token_env = boot_config.server.api_token_env
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
        active = lifecycle.active
        return {
            "status": "ok",
            "service": "cerberus",
            "config_version": active.version,
            "config_checksum": active.checksum,
            "cooldowns": store.snapshot(),
        }

    # -- admin surface (control plane) ------------------------------------

    def admin_denied(request: Request) -> JSONResponse | None:
        if not authenticated(request):
            return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
        return None

    async def admin_body_path(request: Request) -> tuple[str | None, JSONResponse | None]:
        try:
            body = await request.json()
        except json.JSONDecodeError, UnicodeDecodeError:
            return None, JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
        if not isinstance(body, dict):
            return None, JSONResponse(status_code=400, content={"error": {"message": "JSON object body required"}})
        path = body.get("path")
        if path is not None and not isinstance(path, str):
            return None, JSONResponse(status_code=400, content={"error": {"message": "path must be a string"}})
        return path, None

    @app.get("/admin/status", response_model=None)
    async def admin_status(request: Request) -> dict[str, Any] | JSONResponse:
        return admin_denied(request) or lifecycle.status()

    @app.get("/admin/config/active", response_model=None)
    async def admin_config_active(request: Request) -> dict[str, Any] | JSONResponse:
        denied = admin_denied(request)
        if denied is not None:
            return denied
        return lifecycle.active.config.model_dump(mode="json")

    @app.post("/admin/validate")
    async def admin_validate(request: Request) -> JSONResponse:
        denied = admin_denied(request)
        if denied is not None:
            return denied
        path, error = await admin_body_path(request)
        if error is not None or path is None:
            return error or JSONResponse(status_code=400, content={"error": {"message": "path is required"}})
        try:
            candidate = ConfigLifecycle.validate(path)
        except (ValidationError, RuntimeError, OSError, ValueError) as exc:
            return JSONResponse(status_code=422, content={"valid": False, "error": str(exc)})
        return JSONResponse(content={"valid": True, "version": candidate.version, "checksum": candidate.checksum})

    @app.post("/admin/activate")
    async def admin_activate(request: Request) -> JSONResponse:
        denied = admin_denied(request)
        if denied is not None:
            return denied
        path, error = await admin_body_path(request)
        if error is not None or path is None:
            return error or JSONResponse(status_code=400, content={"error": {"message": "path is required"}})
        try:
            activated = lifecycle.activate(path)
        except (ValidationError, RuntimeError, OSError, ValueError) as exc:
            return JSONResponse(status_code=422, content={"activated": False, "error": str(exc)})
        return JSONResponse(content={"activated": True, "active_version": activated.version})

    @app.post("/admin/rollback")
    async def admin_rollback(request: Request) -> JSONResponse:
        denied = admin_denied(request)
        if denied is not None:
            return denied
        try:
            restored = lifecycle.rollback()
        except LookupError as exc:
            return JSONResponse(status_code=409, content={"error": {"message": str(exc)}})
        return JSONResponse(content={"active_version": restored.version})

    @app.post("/admin/shadow")
    async def admin_shadow(request: Request) -> JSONResponse:
        denied = admin_denied(request)
        if denied is not None:
            return denied
        path, error = await admin_body_path(request)
        if error is not None:
            return error
        try:
            shadow = lifecycle.arm_shadow(path)
        except (ValidationError, RuntimeError, OSError, ValueError) as exc:
            return JSONResponse(status_code=422, content={"armed": False, "error": str(exc)})
        return JSONResponse(content={"shadow_version": shadow.version if shadow else None})

    # -- inference surface -------------------------------------------------

    @app.get("/v1/models", response_model=None)
    async def models(request: Request) -> dict[str, Any] | JSONResponse:
        config = lifecycle.active.config
        if config.identities:
            context = await resolve(request, config)
            if context is None:
                return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
            visible = [(name, config.aliases[name]) for name in context.identity.allowed_aliases]
        else:
            if not authenticated(request):
                return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
            visible = list(config.aliases.items())
        return {
            "object": "list",
            "data": [
                {"id": alias_name, "object": "model", "owned_by": "cerberus", "cerberus_mode": alias.mode}
                for alias_name, alias in visible
            ],
        }

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
        document = lifecycle.active
        config = document.config
        context: IdentityContext | None = None
        if config.identities:
            context = await resolve(request, config)
            if context is None:
                return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
        elif not authenticated(request):
            return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
        try:
            body = await request.json()
        except json.JSONDecodeError, UnicodeDecodeError:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "JSON object body required"}})
        alias_name = body.get("model")
        if alias_name is None and context is not None and context.identity.default_alias:
            alias_name = context.identity.default_alias
        if not isinstance(alias_name, str) or alias_name not in config.aliases:
            return JSONResponse(
                status_code=404,
                content={"error": {"message": f"Unknown alias {alias_name!r}; see /v1/models"}},
            )
        alias = config.aliases[alias_name]
        if context is not None:
            denial = authorization_error(context, alias_name, alias)
            if denial is not None:
                telemetry.emit(
                    unauthorized_event(
                        alias_name=alias_name,
                        mode=alias.mode,
                        identity=context.name,
                        config_version=document.version,
                    )
                )
                return JSONResponse(
                    status_code=403,
                    content={"error": {"message": f"Identity not authorized for {alias_name!r}", "reason": denial}},
                )
        shadow = lifecycle.shadow
        if shadow is not None:
            shadow_event = shadow_decision_event(
                document=shadow,
                alias_name=alias_name,
                store=store,
                identity=context.name if context else None,
            )
            if shadow_event is not None:
                telemetry.emit(shadow_event)
        return await dispatch(
            body=body,
            alias_name=alias_name,
            identity_name=context.name if context else None,
            document=document,
            store=store,
            client=request.app.state.http_client,
            telemetry=telemetry,
        )

    return app
