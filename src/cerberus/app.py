"""Cerberus API — thin FastAPI surface over the policy core.

Policy (providers, aliases, identities) hot-swaps through the config lifecycle;
infrastructure bindings (server, telemetry sink, state backend, authentik
issuer) are fixed at boot and change via restart — deliberately, so a config
activation can never silently rebind the process's trust anchors.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import secrets
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
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


def _release_id() -> str:
    """Deployment release identifier stamped on every routing event.

    Production sets CERBERUS_RELEASE_ID (the release-manifest digest). Absent
    that, fall back to a deterministic, explicitly dev-marked identifier so the
    field is never empty and never random.
    """

    configured = os.environ.get("CERBERUS_RELEASE_ID", "").strip()
    if configured:
        return configured
    try:
        version = importlib.metadata.version("cerberus")
    except importlib.metadata.PackageNotFoundError:
        version = "unpackaged"
    return f"dev-{version}"


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_ADMIN_UI_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def _static_asset(name: str) -> str:
    return files("cerberus.ui").joinpath("static").joinpath(name).read_text(encoding="utf-8")


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
    telemetry = TelemetryEmitter(
        boot_config.telemetry,
        telemetry_transport,
        release_id=_release_id(),
    )
    # fail at boot, not first request, if packaging dropped the dashboard assets
    admin_ui_assets = {
        "index.html": ("text/html; charset=utf-8", _static_asset("index.html")),
        "app.css": ("text/css; charset=utf-8", _static_asset("app.css")),
        "app.js": ("text/javascript; charset=utf-8", _static_asset("app.js")),
    }
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

    def token_matches(token_env: str | None, request: Request) -> bool:
        if token_env is None:
            return False
        expected = os.environ.get(token_env, "")
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            return bool(expected) and secrets.compare_digest(supplied, expected)
        except TypeError:
            # non-ASCII header input is a bad credential, never a server error
            return False

    def authenticated(request: Request) -> bool:
        """Inference-surface gate: the api token, or open when none is configured."""

        if boot_config.server.api_token_env is None:
            return True
        return token_matches(boot_config.server.api_token_env, request)

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

    def is_loopback(request: Request) -> bool:
        # transport peer address only — forwarding headers are client-forgeable
        return request.client is not None and request.client.host in _LOOPBACK_HOSTS

    def admin_denied(request: Request, *, read_only: bool = False) -> JSONResponse | None:
        """Loopback-only or admin-scoped credential (PLAN Session 9).

        Read-only surface (dashboard, assets, events, status, active config):
        true loopback peers are always admitted — a local browser cannot attach
        a bearer token — and remote peers need the admin token (or, absent one,
        the api token). Mutating control-plane endpoints require the DISTINCT
        admin credential (server.admin_token_env); the inference api token
        never authorizes mutation, and without an admin credential mutations
        are loopback-only.
        """

        server = boot_config.server
        if read_only:
            if is_loopback(request):
                return None
            # remote read-only access needs the DISTINCT admin credential; the
            # inference api token authorizes inference endpoints only
            if token_matches(server.admin_token_env, request):
                return None
            if server.admin_token_env is not None:
                return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
            return JSONResponse(
                status_code=403, content={"error": {"message": "Admin surface is loopback-only"}}
            )
        # mutations are loopback-only until a scoped Authentik admin flow
        # exists; no bearer credential unlocks them remotely
        if is_loopback(request):
            return None
        return JSONResponse(
            status_code=403, content={"error": {"message": "Admin mutations are loopback-only"}}
        )

    async def admin_body_path(request: Request) -> tuple[str | None, JSONResponse | None]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
        if not isinstance(body, dict):
            return None, JSONResponse(status_code=400, content={"error": {"message": "JSON object body required"}})
        path = body.get("path")
        if path is not None and not isinstance(path, str):
            return None, JSONResponse(status_code=400, content={"error": {"message": "path must be a string"}})
        return path, None

    @app.get("/admin/status", response_model=None)
    async def admin_status(request: Request) -> dict[str, Any] | JSONResponse:
        denied = admin_denied(request, read_only=True)
        if denied is not None:
            return denied
        return {
            **lifecycle.status(),
            "release_id": telemetry.release_id,
            # SPEC §8: worker status belongs on the dashboard; truthful absence
            # until Session 11 deploys the fusion worker — never fabricated health
            "fusion": {"state": "not_configured"},
        }

    @app.get("/admin/config/active", response_model=None)
    async def admin_config_active(request: Request) -> dict[str, Any] | JSONResponse:
        denied = admin_denied(request, read_only=True)
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

    @app.get("/admin/events", include_in_schema=False, response_model=None)
    async def admin_events(request: Request) -> Response:
        denied = admin_denied(request, read_only=True)
        if denied is not None:
            return denied
        return JSONResponse(content={"events": telemetry.recent_events_snapshot()}, headers=_ADMIN_UI_HEADERS)

    def serve_asset(request: Request, name: str) -> Response:
        denied = admin_denied(request, read_only=True)
        if denied is not None:
            return denied
        media_type, body = admin_ui_assets[name]
        return Response(content=body, media_type=media_type, headers=_ADMIN_UI_HEADERS)

    @app.get("/admin/ui", include_in_schema=False, response_model=None)
    async def admin_ui(request: Request) -> Response:
        return serve_asset(request, "index.html")

    @app.get("/admin/ui/app.css", include_in_schema=False, response_model=None)
    async def admin_ui_css(request: Request) -> Response:
        return serve_asset(request, "app.css")

    @app.get("/admin/ui/app.js", include_in_schema=False, response_model=None)
    async def admin_ui_js(request: Request) -> Response:
        return serve_asset(request, "app.js")

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
        except (json.JSONDecodeError, UnicodeDecodeError):
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
