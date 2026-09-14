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
import tempfile
import time
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError

from cerberus.control import ConfigLifecycle
from cerberus.control.admin_fields import apply_updates, build_schema, stage
from cerberus.identity import (
    AuthentikVerifier,
    IdentityContext,
    authorization_error,
    identity_for_client_id,
    resolve_identity,
    supplied_credential,
)
from cerberus.fusion import FusionWorkerClient, fusion_aliases, fusion_dispatch
from cerberus.identity.session_store import InMemorySessionStore, SqliteSessionStore
from cerberus.identity.sso import AdminSSO
from cerberus.registry import CerberusConfig, ConfigDocument, load_config_document
from cerberus.registry.schema import Alias, Candidate
from cerberus.router.dispatch import dispatch, shadow_decision_event, unauthorized_event
from cerberus.state import InMemoryCooldownStore, SqliteCooldownStore
from cerberus.telemetry import TelemetryEmitter

SESSION_COOKIE = "cerberus_admin_session"


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


def free_provider_models(config: CerberusConfig) -> list[dict[str, str]]:
    """Every free provider model as a routable ``provider/model`` id.

    Exposed alongside aliases so clients get a rich picker (operator choice);
    direct calls to these are still free-only enforced. Paid models are omitted.
    """

    out: list[dict[str, str]] = []
    for provider_name, provider in config.providers.items():
        for model_name, model in provider.models.items():
            if model.cost_tier == "free":
                out.append({"id": f"{provider_name}/{model_name}", "provider": provider_name})
    return out


def resolve_direct_model(config: CerberusConfig, model_id: str) -> tuple[Alias, str] | None:
    """Resolve a raw ``provider/model`` id into a synthetic single-candidate free
    alias, or None if it is not a known free provider model. Splits on the first
    '/', so models that themselves contain '/' (e.g. cloudflare @cf/...) work."""

    provider_name, _, model_name = model_id.partition("/")
    provider = config.providers.get(provider_name)
    if provider is None or not model_name:
        return None
    model = provider.models.get(model_name)
    if model is None or model.cost_tier != "free":
        return None
    credential = next(iter(provider.credentials))  # first credential
    alias = Alias(
        mode="free",
        candidates=[Candidate(provider=provider_name, credential=credential, model=model_name)],
    )
    return alias, credential


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
    fusion_transport: httpx.AsyncBaseTransport | None = None,
    sso_transport: httpx.AsyncBaseTransport | None = None,
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
    # staged candidate configs from the browser editor land here — the same
    # writable volume as the state DB in production; a tmp fallback keeps
    # tests/dry-runs (state.path=None) working without touching disk state
    admin_staging_dir = (
        str(Path(state_path).parent / "staging") if state_path else str(Path(tempfile.gettempdir()) / "cerberus-admin-staging")
    )
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
    fusion_worker = FusionWorkerClient(boot_config.fusion_worker, fusion_transport)
    if boot_config.admin_sso is not None:
        # sessions + pending logins share the state DB when one is configured, so
        # they survive restarts and are consistent across workers; else in-memory
        session_store = SqliteSessionStore(state_path) if state_path else InMemorySessionStore()
        admin_sso = AdminSSO(boot_config.admin_sso, sso_transport, store=session_store)
    else:
        admin_sso = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient(transport=http_transport, timeout=300.0)
        await telemetry.start()
        try:
            yield
        finally:
            await telemetry.close()
            await app.state.http_client.aclose()
            await fusion_worker.aclose()
            if verifier is not None:
                await verifier.aclose()
            if admin_sso is not None:
                await admin_sso.aclose()

    # The built-in doc routes are unconditionally public, so they are disabled here
    # and re-served below through admin_gate (unless server.public_docs opts out).
    public_docs = boot_config.server.public_docs
    app = FastAPI(
        title="Cerberus",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if public_docs else None,
        redoc_url="/redoc" if public_docs else None,
        openapi_url="/openapi.json" if public_docs else None,
    )
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
    async def root(request: Request) -> RedirectResponse:
        # with SSO configured, land on the console when signed in, else the login
        if admin_sso is not None:
            session = admin_sso.session_from_cookie(request.cookies.get(SESSION_COOKIE))
            return RedirectResponse(url="/admin/ui" if session is not None else "/admin/login")
        return RedirectResponse(url="/docs")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        active = lifecycle.active
        return {
            "status": "ok",
            "service": "cerberus",
            "routing": {"status": "healthy"},
            "telemetry": telemetry.health_snapshot(),
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

    async def admin_gate(request: Request, *, read_only: bool = False) -> JSONResponse | None:
        """Admin authorization. When SSO is configured: a valid Authentik-backed
        browser session (signed cookie), an Authentik admin-scoped break-glass
        bearer, or a true loopback peer authorizes; anything else gets 401 to
        prompt the OIDC login. Without SSO, the loopback/token rules apply."""

        if admin_sso is not None:
            if admin_sso.session_from_cookie(request.cookies.get(SESSION_COOKIE)) is not None:
                return None
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            # a JWT has two dots; the static admin token never does
            if bearer.count(".") == 2 and await admin_sso.verify_break_glass(bearer):
                return None
            if is_loopback(request):
                return None
            return JSONResponse(status_code=401, content={"error": {"message": "Authentication required — /admin/login"}})
        return admin_denied(request, read_only=read_only)

    def require_csrf(request: Request) -> JSONResponse | None:
        """A custom header a cross-site form/img cannot set, so a page riding an
        ambient SSO session cookie can't trigger a mutation via a simple request
        (anything else is CORS-preflighted, and CSP already forbids cross-origin
        connects). Same check the provider probe already uses (R4); applied here
        to every state-mutating /admin/* POST now that cookie sessions exist."""

        if request.headers.get("x-cerberus-csrf") != "1":
            return JSONResponse(
                status_code=403,
                content={"error": {"message": "Missing X-Cerberus-CSRF header"}},
                headers=_ADMIN_UI_HEADERS,
            )
        return None

    if not public_docs:
        # Re-served behind the same boundary as the rest of /admin: the schema names
        # every admin route and its shape, so it is admin information itself.
        @app.get("/openapi.json", include_in_schema=False)
        async def openapi_schema(request: Request) -> Response:
            denied = await admin_gate(request, read_only=True)
            return denied if denied is not None else JSONResponse(app.openapi())

        @app.get("/docs", include_in_schema=False)
        async def swagger_ui(request: Request) -> Response:
            denied = await admin_gate(request, read_only=True)
            if denied is not None:
                return denied
            return get_swagger_ui_html(openapi_url="/openapi.json", title="Cerberus API")

    # -- Authentik OIDC login for the console -----------------------------

    @app.get("/admin/login", include_in_schema=False, response_model=None)
    async def admin_login(request: Request) -> Response:
        if admin_sso is None:
            return JSONResponse(status_code=404, content={"error": {"message": "SSO not configured"}})
        if admin_sso.session_from_cookie(request.cookies.get(SESSION_COOKIE)) is not None:
            return RedirectResponse(url="/admin/ui")
        return Response(
            content=_static_asset("login.html"), media_type="text/html; charset=utf-8",
            headers=_ADMIN_UI_HEADERS,
        )

    @app.get("/admin/login/start", include_in_schema=False, response_model=None)
    async def admin_login_start() -> Response:
        if admin_sso is None:
            return JSONResponse(status_code=404, content={"error": {"message": "SSO not configured"}})
        return RedirectResponse(url=admin_sso.start_login(), status_code=302)

    @app.get("/admin/callback", include_in_schema=False, response_model=None)
    async def admin_callback(request: Request) -> Response:
        if admin_sso is None:
            return JSONResponse(status_code=404, content={"error": {"message": "SSO not configured"}})
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        session = await admin_sso.complete_login(code, state)
        if session is None:
            return JSONResponse(status_code=403, content={"error": {"message": "SSO login failed or not an admin"}})
        sid = admin_sso.create_session(session)
        resp = RedirectResponse(url="/admin/ui", status_code=302)
        resp.set_cookie(
            SESSION_COOKIE, admin_sso.cookie_value(sid),
            max_age=boot_config.admin_sso.session_ttl_seconds if boot_config.admin_sso else 3600,
            httponly=True, secure=True, samesite="lax", path="/",
        )
        return resp

    @app.get("/admin/logout", include_in_schema=False, response_model=None)
    async def admin_logout(request: Request) -> Response:
        if admin_sso is not None:
            admin_sso.logout(request.cookies.get(SESSION_COOKIE))
        resp = RedirectResponse(url="/admin/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

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
        denied = await admin_gate(request, read_only=True)
        if denied is not None:
            return denied
        return {
            **lifecycle.status(),
            "release_id": telemetry.release_id,
            # SPEC §8: worker status on the dashboard. "configured" means the
            # binding is present (a fusion request will reach the worker); we do
            # not probe worker health from this read-only endpoint.
            "fusion": {
                "state": "configured" if fusion_worker.configured else "not_configured",
                "aliases": fusion_aliases(boot_config),
            },
        }

    @app.get("/admin/config/active", response_model=None)
    async def admin_config_active(request: Request) -> dict[str, Any] | JSONResponse:
        denied = await admin_gate(request, read_only=True)
        if denied is not None:
            return denied
        return lifecycle.active.config.model_dump(mode="json")

    @app.get("/admin/config/schema", response_model=None)
    async def admin_config_schema(request: Request) -> dict[str, Any] | JSONResponse:
        denied = await admin_gate(request, read_only=True)
        if denied is not None:
            return denied
        return build_schema(lifecycle.active.config)

    @app.post("/admin/config/stage")
    async def admin_config_stage(request: Request) -> JSONResponse:
        denied = await admin_gate(request)
        if denied is not None:
            return denied
        denied = require_csrf(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
        updates = body.get("updates") if isinstance(body, dict) else None
        if not isinstance(updates, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "updates object is required"}})
        try:
            candidate = apply_updates(lifecycle.active.config, updates)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"staged": False, "error": str(exc)})
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"staged": False, "error": str(exc)})
        path = stage(candidate, admin_staging_dir)
        return JSONResponse(content={"staged": True, "path": path, "version": candidate.metadata.version})

    @app.post("/admin/validate")
    async def admin_validate(request: Request) -> JSONResponse:
        denied = await admin_gate(request)
        if denied is not None:
            return denied
        denied = require_csrf(request)
        if denied is not None:
            return denied
        path, error = await admin_body_path(request)
        if error is not None or path is None:
            return error or JSONResponse(status_code=400, content={"error": {"message": "path is required"}})
        try:
            candidate = lifecycle.validate(path)
        except (ValidationError, RuntimeError, OSError, ValueError, yaml.YAMLError) as exc:
            return JSONResponse(status_code=422, content={"valid": False, "error": str(exc)})
        return JSONResponse(content={"valid": True, "version": candidate.version, "checksum": candidate.checksum})

    @app.post("/admin/activate")
    async def admin_activate(request: Request) -> JSONResponse:
        denied = await admin_gate(request)
        if denied is not None:
            return denied
        denied = require_csrf(request)
        if denied is not None:
            return denied
        path, error = await admin_body_path(request)
        if error is not None or path is None:
            return error or JSONResponse(status_code=400, content={"error": {"message": "path is required"}})
        try:
            activated = lifecycle.activate(path)
        except (ValidationError, RuntimeError, OSError, ValueError, yaml.YAMLError) as exc:
            return JSONResponse(status_code=422, content={"activated": False, "error": str(exc)})
        return JSONResponse(content={"activated": True, "active_version": activated.version})

    @app.post("/admin/rollback")
    async def admin_rollback(request: Request) -> JSONResponse:
        denied = await admin_gate(request)
        if denied is not None:
            return denied
        denied = require_csrf(request)
        if denied is not None:
            return denied
        try:
            restored = lifecycle.rollback()
        except LookupError as exc:
            return JSONResponse(status_code=409, content={"error": {"message": str(exc)}})
        return JSONResponse(content={"active_version": restored.version})

    @app.post("/admin/shadow")
    async def admin_shadow(request: Request) -> JSONResponse:
        denied = await admin_gate(request)
        if denied is not None:
            return denied
        denied = require_csrf(request)
        if denied is not None:
            return denied
        path, error = await admin_body_path(request)
        if error is not None:
            return error
        try:
            shadow = lifecycle.arm_shadow(path)
        except (ValidationError, RuntimeError, OSError, ValueError, yaml.YAMLError) as exc:
            return JSONResponse(status_code=422, content={"armed": False, "error": str(exc)})
        return JSONResponse(content={"shadow_version": shadow.version if shadow else None})

    @app.get("/admin/events", include_in_schema=False, response_model=None)
    async def admin_events(request: Request) -> Response:
        denied = await admin_gate(request, read_only=True)
        if denied is not None:
            return denied
        return JSONResponse(content={"events": telemetry.recent_events_snapshot()}, headers=_ADMIN_UI_HEADERS)

    @app.get("/admin/providers", include_in_schema=False, response_model=None)
    async def admin_providers(request: Request) -> Response:
        denied = await admin_gate(request, read_only=True)
        if denied is not None:
            return denied
        config = lifecycle.active.config
        cooled = {(c["provider"], c.get("model")) for c in store.snapshot()}
        out = []
        for name, provider in config.providers.items():
            envs = sorted({cred.api_key_env for cred in provider.credentials.values()})
            configured = all(os.environ.get(e, "").strip() for e in envs)
            out.append(
                {
                    "name": name,
                    "base_url": str(provider.base_url),
                    "credential_envs": envs,  # names only, never values
                    "configured": configured,
                    "models": [
                        {"id": m, "cost_tier": entry.cost_tier} for m, entry in provider.models.items()
                    ],
                    "cooled_down": any(p == name for p, _ in cooled),
                }
            )
        return JSONResponse(content={"providers": out}, headers=_ADMIN_UI_HEADERS)

    @app.post("/admin/providers/{name}/test", include_in_schema=False, response_model=None)
    async def admin_provider_test(name: str, request: Request) -> Response:
        """Live provider probe. POST (not GET) because it spends the provider's
        quota — an operational action, not a read — and it requires a custom
        header so a cross-site form/img cannot trigger it once cookie sessions
        exist (simple requests cannot set custom headers; anything else is
        preflighted, and CSP already forbids cross-origin connects)."""

        denied = await admin_gate(request, read_only=True)
        if denied is not None:
            return denied
        denied = require_csrf(request)
        if denied is not None:
            return denied
        config = lifecycle.active.config
        provider = config.providers.get(name)
        if provider is None:
            return JSONResponse(status_code=404, content={"error": {"message": f"Unknown provider {name!r}"}})
        cred = next(iter(provider.credentials.values()))
        key = os.environ.get(cred.api_key_env, "").strip()
        if not key:
            return JSONResponse(content={"ok": False, "reason": "missing_key"}, headers=_ADMIN_UI_HEADERS)
        base = str(provider.base_url).rstrip("/")
        auth = {"authorization": f"Bearer {key}"}
        client = request.app.state.http_client
        started = time.perf_counter()
        try:
            if provider.health_probe == "chat":
                # providers that do not serve GET /models (e.g. Cloudflare Workers AI)
                model = next(iter(provider.models))
                resp = await client.post(
                    f"{base}/chat/completions",
                    headers=auth,
                    json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                    timeout=15.0,
                )
            else:
                resp = await client.get(f"{base}/models", headers=auth, timeout=10.0)
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            return JSONResponse(
                content={
                    "ok": resp.status_code < 400,
                    "status": resp.status_code,
                    "latency_ms": latency_ms,
                    "probe": provider.health_probe,
                },
                headers=_ADMIN_UI_HEADERS,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                content={"ok": False, "reason": type(exc).__name__}, headers=_ADMIN_UI_HEADERS
            )

    async def serve_asset(request: Request, name: str) -> Response:
        denied = await admin_gate(request, read_only=True)
        if denied is not None:
            return denied
        media_type, body = admin_ui_assets[name]
        return Response(content=body, media_type=media_type, headers=_ADMIN_UI_HEADERS)

    @app.get("/admin/ui", include_in_schema=False, response_model=None)
    async def admin_ui(request: Request) -> Response:
        return await serve_asset(request, "index.html")

    @app.get("/admin/ui/app.css", include_in_schema=False, response_model=None)
    async def admin_ui_css(request: Request) -> Response:
        return await serve_asset(request, "app.css")

    @app.get("/admin/ui/app.js", include_in_schema=False, response_model=None)
    async def admin_ui_js(request: Request) -> Response:
        return await serve_asset(request, "app.js")

    # -- inference surface -------------------------------------------------

    @app.get("/v1/models", response_model=None)
    async def models(request: Request) -> dict[str, Any] | JSONResponse:
        config = lifecycle.active.config
        if config.identities:
            context = await resolve(request, config)
            if context is None:
                return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
            visible = [(name, config.aliases[name]) for name in context.identity.allowed_aliases]
            allow_direct = context.identity.allow_direct_models
        else:
            if not authenticated(request):
                return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
            visible = list(config.aliases.items())
            allow_direct = True  # server-token mode is the operator surface
        data: list[dict[str, Any]] = [
            {"id": alias_name, "object": "model", "owned_by": "cerberus", "cerberus_mode": alias.mode}
            for alias_name, alias in visible
        ]
        if allow_direct:
            # rich picker: raw free provider models directly routable (opt-in only, free-only)
            data.extend(
                {"id": m["id"], "object": "model", "owned_by": m["provider"], "cerberus_mode": "direct"}
                for m in free_provider_models(config)
            )
        return {"object": "list", "data": data}

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
        # raw provider/model ids route as ad-hoc free requests when not a named alias
        direct = resolve_direct_model(config, alias_name) if isinstance(alias_name, str) else None
        if not isinstance(alias_name, str) or (alias_name not in config.aliases and direct is None):
            return JSONResponse(
                status_code=404,
                content={"error": {"message": f"Unknown model {alias_name!r}; see /v1/models"}},
            )
        if direct is not None:
            # direct free-model routing is a per-identity opt-in (allow_direct_models)
            # AND requires free mode. Absent the opt-in, identities keep least
            # privilege to their allowed_aliases — the model is simply "unknown".
            if context is not None and not context.identity.allow_direct_models:
                return JSONResponse(
                    status_code=404,
                    content={"error": {"message": f"Unknown model {alias_name!r}; see /v1/models"}},
                )
            alias, _ = direct
            if context is not None and "free" not in context.identity.allowed_modes:
                telemetry.emit(
                    unauthorized_event(
                        alias_name=alias_name, mode="free", identity=context.name, config_version=document.version
                    )
                )
                return JSONResponse(
                    status_code=403,
                    content={"error": {"message": "Identity not authorized for free mode", "reason": "mode_not_allowed"}},
                )
            # register the synthetic alias so the shared routing path resolves it
            synth_config = config.model_copy(update={"aliases": {**config.aliases, alias_name: alias}})
            document = ConfigDocument(
                config=synth_config, version=document.version, checksum=document.checksum,
                source_path=document.source_path,
            )
            config = synth_config
        else:
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
        if alias.mode == "fusion":
            # fusion-mode aliases fan out to the bundled worker; a worker outage
            # fails only fusion aliases, never dispatch/free (fail closed).
            return await fusion_dispatch(
                body=body,
                alias_name=alias_name,
                alias=alias,
                identity_name=context.name if context else None,
                document=document,
                worker=fusion_worker,
                telemetry=telemetry,
            )
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
