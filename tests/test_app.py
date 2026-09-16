"""Session 2 acceptance tests — the Cerberus API on the frozen schema (donor scenarios ported)."""

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import CerberusConfig


def make_config(monkeypatch: pytest.MonkeyPatch, **server) -> CerberusConfig:
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("BETA_KEY", "beta-secret")
    return CerberusConfig.model_validate(
        {
            "metadata": {"version": "cerberus-2026-07-16.1"},
            "server": server or {"host": "127.0.0.1", "port": 4000},
            "providers": {
                "alpha": {
                    "base_url": "https://alpha.example/v1",
                    "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                    "models": {"alpha-free": {"cost_tier": "free"}},
                },
                "beta": {
                    "base_url": "https://beta.example/v1",
                    "credentials": {"main": {"api_key_env": "BETA_KEY"}},
                    "models": {"beta-free": {"cost_tier": "free"}, "beta-pro": {"cost_tier": "paid"}},
                },
            },
            "aliases": {
                "cerberus/main": {
                    "mode": "dispatch",
                    "candidates": [
                        {"provider": "alpha", "credential": "main", "model": "alpha-free"},
                        {"provider": "beta", "credential": "main", "model": "beta-free"},
                    ],
                },
                "cerberus/frugal": {
                    "mode": "dispatch",
                    "allow_paid_fallback": False,
                    "candidates": [
                        {"provider": "beta", "credential": "main", "model": "beta-pro"},
                        {"provider": "beta", "credential": "main", "model": "beta-free"},
                    ],
                },
            },
        }
    )


async def call(app, method: str, path: str, **kwargs) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_routes_first_candidate_and_redacts_credentials(monkeypatch):
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(app, "POST", "/v1/chat/completions", json={"model": "cerberus/main", "messages": []})

    assert response.status_code == 200
    meta = response.json()["cerberus"]
    assert (meta["provider"], meta["credential"], meta["model"]) == ("alpha", "main", "alpha-free")
    assert meta["alias"] == "cerberus/main" and meta["attempts"] == 1
    assert "alpha-secret" not in response.text
    assert seen[0].url == "https://alpha.example/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer alpha-secret"
    body = httpx.Response(200, content=seen[0].content).json()
    assert body["model"] == "alpha-free"


@pytest.mark.asyncio
async def test_rate_limit_fails_over_in_declared_order_and_cools_down(monkeypatch):
    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "alpha.example":
            return httpx.Response(429, headers={"retry-after": "10"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fallback"}}]})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={"model": "cerberus/main", "messages": []})
            health = await client.get("/admin/health")  # cooldowns are admin-only diagnostics

    assert response.status_code == 200
    meta = response.json()["cerberus"]
    assert (meta["provider"], meta["model"], meta["attempts"]) == ("beta", "beta-free", 2)
    cooldowns = health.json()["cooldowns"]
    assert cooldowns[0]["provider"] == "alpha" and cooldowns[0]["scope"] == "model"
    assert cooldowns[0]["reason"] == "quota_429"


@pytest.mark.asyncio
async def test_paid_fallback_prohibited_skips_paid_candidate(monkeypatch):
    async def upstream(request: httpx.Request) -> httpx.Response:
        body = httpx.Response(200, content=request.content).json()
        assert body["model"] == "beta-free", "paid target must never be invoked"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(app, "POST", "/v1/chat/completions", json={"model": "cerberus/frugal", "messages": []})

    assert response.status_code == 200
    assert response.json()["cerberus"]["model"] == "beta-free"


@pytest.mark.asyncio
async def test_streaming_response_is_proxied(monkeypatch):
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: hello\n\ndata: [DONE]\n\n"
        )

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(app, "POST", "/v1/chat/completions", json={"model": "cerberus/main", "messages": [], "stream": True})

    assert response.status_code == 200
    assert response.headers["x-cerberus-provider"] == "alpha"
    assert response.headers["x-cerberus-model"] == "alpha-free"
    assert response.text == "data: hello\n\ndata: [DONE]\n\n"


@pytest.mark.asyncio
async def test_unknown_alias_is_rejected_without_upstream_call(monkeypatch):
    calls = 0

    async def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(app, "POST", "/v1/chat/completions", json={"model": "gpt-4o", "messages": []})

    assert response.status_code == 404
    assert "gpt-4o" in response.json()["error"]["message"]
    assert calls == 0


@pytest.mark.asyncio
async def test_exhausted_candidates_return_503_with_request_id(monkeypatch):
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(app, "POST", "/v1/chat/completions", json={"model": "cerberus/main", "messages": []})

    assert response.status_code == 503
    payload = response.json()["error"]
    assert payload["request_id"]
    assert payload["message"] == "No provider available"


@pytest.mark.asyncio
async def test_malformed_json_returns_400_without_upstream_call(monkeypatch):
    calls = 0

    async def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(
        app, "POST", "/v1/chat/completions", content=b'{"messages":', headers={"content-type": "application/json"}
    )

    assert response.status_code == 400
    assert calls == 0


@pytest.mark.asyncio
async def test_models_endpoint_lists_aliases(monkeypatch):
    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    response = await call(app, "GET", "/v1/models")

    assert response.status_code == 200
    data = response.json()["data"]
    ids = {entry["id"] for entry in data}
    # aliases plus directly-routable free provider models (paid beta-pro excluded)
    assert {"cerberus/main", "cerberus/frugal"} <= ids
    assert {"alpha/alpha-free", "beta/beta-free"} <= ids
    assert "beta/beta-pro" not in ids
    modes = {entry["id"]: entry["cerberus_mode"] for entry in data}
    assert modes["cerberus/main"] == "dispatch"
    assert modes["alpha/alpha-free"] == "direct"


@pytest.mark.asyncio
async def test_health_reports_version_and_checksumless_state(monkeypatch):
    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    response = await call(app, "GET", "/admin/health")

    payload = response.json()
    assert payload["status"] == "ok" and payload["service"] == "cerberus"
    assert payload["config_version"] == "cerberus-2026-07-16.1"


@pytest.mark.asyncio
async def test_external_bind_rejects_unauthenticated_requests(monkeypatch):
    monkeypatch.setenv("CERBERUS_API_TOKEN", "router-token")
    config = make_config(monkeypatch, host="0.0.0.0", port=4000, api_token_env="CERBERUS_API_TOKEN")
    app = create_app(config, http_transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorized = await client.get("/v1/models")
            authorized = await client.get("/v1/models", headers={"authorization": "Bearer router-token"})
            non_ascii = await client.get(
                "/v1/models", headers={b"authorization": "Bearer routér".encode("latin-1")}
            )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert non_ascii.status_code == 401  # never a 500 (v3 review finding)


@pytest.mark.asyncio
async def test_direct_free_provider_model_routes(monkeypatch):
    """A raw provider/model id routes as an ad-hoc free request (rich-picker path)."""
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(app, "POST", "/v1/chat/completions", json={"model": "alpha/alpha-free", "messages": []})
    assert response.status_code == 200
    meta = response.json()["cerberus"]
    assert meta["provider"] == "alpha" and meta["model"] == "alpha-free" and meta["mode"] == "free"


@pytest.mark.asyncio
async def test_direct_paid_provider_model_is_rejected(monkeypatch):
    """Free-only guarantee holds for direct routing: a paid provider model is not routable."""
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(app, "POST", "/v1/chat/completions", json={"model": "beta/beta-pro", "messages": []})
    assert response.status_code == 404  # paid model is not an exposed direct target


def _identity_config(monkeypatch, *, allow_direct):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CB_KEY_X", "cb-x")
    return CerberusConfig.model_validate({
        "metadata": {"version": "cerberus-2026-07-16.1"},
        "providers": {"alpha": {"base_url": "https://alpha.example/v1",
            "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
            "models": {"alpha-free": {"cost_tier": "free"}}}},
        "identities": {"x": {"credential_env": "CB_KEY_X", "allowed_modes": ["free"],
            "allowed_aliases": ["cerberus/x"], "allow_direct_models": allow_direct}},
        "aliases": {"cerberus/x": {"mode": "free",
            "candidates": [{"provider": "alpha", "credential": "main", "model": "alpha-free"}]}},
    })


@pytest.mark.asyncio
async def test_direct_models_require_per_identity_optin(monkeypatch):
    async def upstream(_r): return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    auth = {"authorization": "Bearer cb-x"}
    # scoped identity: no allow_direct_models -> aliases only, direct route is 404
    app = create_app(_identity_config(monkeypatch, allow_direct=False), http_transport=httpx.MockTransport(upstream))
    models = await call(app, "GET", "/v1/models", headers=auth)
    assert {m["id"] for m in models.json()["data"]} == {"cerberus/x"}
    denied = await call(app, "POST", "/v1/chat/completions", json={"model": "alpha/alpha-free", "messages": []}, headers=auth)
    assert denied.status_code == 404
    # opted-in identity: sees the raw model and may route it
    app2 = create_app(_identity_config(monkeypatch, allow_direct=True), http_transport=httpx.MockTransport(upstream))
    models2 = await call(app2, "GET", "/v1/models", headers=auth)
    assert "alpha/alpha-free" in {m["id"] for m in models2.json()["data"]}
    ok = await call(app2, "POST", "/v1/chat/completions", json={"model": "alpha/alpha-free", "messages": []}, headers=auth)
    assert ok.status_code == 200 and ok.json()["cerberus"]["provider"] == "alpha"


def _colliding_alias_config(monkeypatch):
    """A provider named "cerberus" makes every alias name also parse as a direct
    provider/model id, because aliases carry the "cerberus/" prefix."""

    monkeypatch.setenv("CKEY", "ckey-secret")
    monkeypatch.setenv("CB_KEY_SCOPED", "cb-scoped")
    return CerberusConfig.model_validate({
        "metadata": {"version": "cerberus-2026-07-16.1"},
        "providers": {"cerberus": {"base_url": "https://up.example/v1",
            "credentials": {"main": {"api_key_env": "CKEY"}},
            "models": {"restricted": {"cost_tier": "free"}}}},
        "identities": {"scoped": {"credential_env": "CB_KEY_SCOPED", "allowed_modes": ["free"],
            "allowed_aliases": ["cerberus/allowed"], "allow_direct_models": True}},
        "aliases": {
            "cerberus/allowed": {"mode": "free",
                "candidates": [{"provider": "cerberus", "credential": "main", "model": "restricted"}]},
            # name also parses as provider "cerberus" + free model "restricted"
            "cerberus/restricted": {"mode": "dispatch",
                "candidates": [{"provider": "cerberus", "credential": "main", "model": "restricted"}]},
        },
    })


@pytest.mark.asyncio
async def test_named_alias_takes_precedence_over_direct_resolution(monkeypatch):
    """A configured alias is never reachable through the direct provider/model path.

    Resolving direct first let an identity invoke an alias absent from its
    allowed_aliases: the direct branch only checks allow_direct_models and free
    mode, so authorization_error() — and with it the allow-list — never ran.
    """

    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(_colliding_alias_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    auth = {"authorization": "Bearer cb-scoped"}

    denied = await call(app, "POST", "/v1/chat/completions",
                        json={"model": "cerberus/restricted", "messages": []}, headers=auth)
    assert denied.status_code == 403
    assert denied.json()["error"]["reason"] == "alias_not_allowed"
    assert seen == []  # the alias must never reach its upstream

    # the identity's own alias still routes, and direct ids are unaffected
    allowed = await call(app, "POST", "/v1/chat/completions",
                         json={"model": "cerberus/allowed", "messages": []}, headers=auth)
    assert allowed.status_code == 200 and len(seen) == 1


@pytest.mark.parametrize("model_id", ["gpt-4o-mini", "operator/custom-model-2026"])
@pytest.mark.parametrize("stream", [False, True])
async def test_configured_models_route_without_a_catalog(monkeypatch, model_id, stream):
    """Configured model IDs and provider payloads need no bundled model catalog."""
    cfg = make_config(monkeypatch).model_dump(mode="json")
    cfg["providers"]["alpha"]["models"] = {model_id: {"cost_tier": "free", "context_window": 8192}}
    cfg["aliases"]["cerberus/main"]["candidates"][0]["model"] = model_id
    seen = []
    payload = {
        "model": "cerberus/main",
        "messages": [{"role": "user", "content": "test"}],
        "stream": stream,
        "temperature": 0.25,
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    }
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}

    async def upstream(request):
        seen.append(request)
        assert request.url == "https://alpha.example/v1/chat/completions"
        assert httpx.Response(200, content=request.content).json() == {**payload, "model": model_id}
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": usage})

    app = create_app(CerberusConfig.model_validate(cfg), http_transport=httpx.MockTransport(upstream))
    response = await call(app, "POST", "/v1/chat/completions", json=payload)
    assert response.status_code == 200
    assert len(seen) == 1  # No metadata discovery request before inference.
    if stream:
        assert response.text == "data: [DONE]\n\n"
        assert response.headers["x-cerberus-model"] == model_id
    else:
        assert response.json()["usage"] == usage
        assert response.json()["cerberus"]["model"] == model_id


@pytest.mark.asyncio
async def test_cooldown_topology_is_never_public(monkeypatch):
    """The cooldown snapshot names provider, credential and model for every
    cooled target. That is routing topology, and /health has no gate at all, so
    it must not appear there even while a cooldown is active."""

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "alpha.example":
            return httpx.Response(429, headers={"retry-after": "10"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fallback"}}]})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/v1/chat/completions", json={"model": "cerberus/main", "messages": []})
            public = await client.get("/health")
            admin = await client.get("/admin/health")

    # precondition: a cooldown really is active, so this is not a vacuous check
    assert admin.json()["cooldowns"], "expected the 429 to have cooled alpha down"
    assert admin.json()["cooldowns"][0]["provider"] == "alpha"

    assert public.json() == {"status": "ok", "service": "cerberus"}
    for leaked in ("alpha", "beta", "alpha-free", "quota_429", "cerberus-2026-07-16.1"):
        assert leaked not in public.text, f"public /health leaked {leaked!r}"
