"""Session 4 acceptance tests — cb- client keys, deny-by-default authorization (SPEC test 12)."""

import json

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import CerberusConfig


def identity_config(monkeypatch: pytest.MonkeyPatch, tmp_path=None) -> CerberusConfig:
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CB_KEY_RECON", "cb-recon-key")
    monkeypatch.setenv("CB_KEY_CODER", "cb-coder-key")
    return CerberusConfig.model_validate(
        {
            "metadata": {"version": "cerberus-2026-07-16.1"},
            "providers": {
                "alpha": {
                    "base_url": "https://alpha.example/v1",
                    "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                    "models": {
                        "alpha-free": {"cost_tier": "free"},
                        "alpha-code": {"cost_tier": "free"},
                    },
                },
            },
            "aliases": {
                "cerberus/free": {
                    "mode": "free",
                    "candidates": [{"provider": "alpha", "credential": "main", "model": "alpha-free"}],
                },
                "cerberus/dispatch-code": {
                    "mode": "dispatch",
                    "candidates": [{"provider": "alpha", "credential": "main", "model": "alpha-code"}],
                },
            },
            "identities": {
                "recon": {
                    "credential_env": "CB_KEY_RECON",
                    "allowed_modes": ["free"],
                    "allowed_aliases": ["cerberus/free"],
                    "default_alias": "cerberus/free",
                },
                "coder": {
                    "credential_env": "CB_KEY_CODER",
                    "allowed_modes": ["free", "dispatch"],
                    "allowed_aliases": ["cerberus/free", "cerberus/dispatch-code"],
                },
            },
        }
    )


def ok_upstream(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


async def post(app, body, headers=None) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/v1/chat/completions", json=body, headers=headers or {})


@pytest.mark.asyncio
async def test_bearer_and_x_api_key_resolve_the_same_identity(monkeypatch):
    app = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(ok_upstream))
    body = {"model": "cerberus/free", "messages": []}
    via_bearer = await post(app, body, {"authorization": "Bearer cb-recon-key"})
    app2 = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(ok_upstream))
    via_api_key = await post(app2, body, {"x-api-key": "cb-recon-key"})
    assert via_bearer.status_code == 200 and via_api_key.status_code == 200
    assert via_bearer.json()["cerberus"]["identity"] == "recon"
    assert via_api_key.json()["cerberus"]["identity"] == "recon"


@pytest.mark.asyncio
async def test_unknown_credential_is_401_without_upstream_call(monkeypatch):
    calls = 0

    def upstream(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    app = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await post(app, {"model": "cerberus/free", "messages": []}, {"authorization": "Bearer cb-wrong"})
    assert response.status_code == 401
    assert calls == 0


@pytest.mark.asyncio
async def test_missing_credential_is_401_when_identities_configured(monkeypatch):
    app = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(ok_upstream))
    response = await post(app, {"model": "cerberus/free", "messages": []})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_allowed_alias_is_403_and_telemetried_with_identity(monkeypatch, tmp_path):
    """SPEC acceptance test 12."""
    config = identity_config(monkeypatch)
    token_file = tmp_path / "token"
    token_file.write_text("telemetry-token", encoding="utf-8")
    config = config.model_copy(
        update={
            "telemetry": config.telemetry.model_copy(
                update={
                    "endpoint": "http://contextforge.test/v1/telemetry/routing-records",
                    "bearer_token_file": str(token_file),
                    "timeout_seconds": 0.2,
                }
            )
        }
    )
    events: list[dict] = []

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201)

    upstream_calls = 0

    def upstream(_request):
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    response = await post(
        app, {"model": "cerberus/dispatch-code", "messages": []}, {"authorization": "Bearer cb-recon-key"}
    )

    assert response.status_code == 403
    assert upstream_calls == 0
    assert len(events) == 1
    assert events[0]["outcome"] == "unauthorized"
    assert events[0]["identity"] == "recon"
    assert events[0]["alias"] == "cerberus/dispatch-code"


@pytest.mark.asyncio
async def test_allowed_alias_routes_and_metadata_carries_identity(monkeypatch):
    app = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(ok_upstream))
    response = await post(
        app, {"model": "cerberus/dispatch-code", "messages": []}, {"authorization": "Bearer cb-coder-key"}
    )
    assert response.status_code == 200
    assert response.json()["cerberus"]["identity"] == "coder"


@pytest.mark.asyncio
async def test_default_alias_used_when_model_omitted(monkeypatch):
    app = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(ok_upstream))
    response = await post(app, {"messages": []}, {"authorization": "Bearer cb-recon-key"})
    assert response.status_code == 200
    assert response.json()["cerberus"]["alias"] == "cerberus/free"


@pytest.mark.asyncio
async def test_no_default_alias_and_no_model_is_client_error(monkeypatch):
    app = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(ok_upstream))
    response = await post(app, {"messages": []}, {"authorization": "Bearer cb-coder-key"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_models_endpoint_is_scoped_to_the_identity(monkeypatch):
    app = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(ok_upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            recon = await client.get("/v1/models", headers={"authorization": "Bearer cb-recon-key"})
            coder = await client.get("/v1/models", headers={"x-api-key": "cb-coder-key"})
            anonymous = await client.get("/v1/models")

    # least privilege: without allow_direct_models an identity sees only its aliases,
    # never raw provider models (the rich-picker catalog is opt-in per identity).
    assert {m["id"] for m in recon.json()["data"]} == {"cerberus/free"}
    assert {m["id"] for m in coder.json()["data"]} == {"cerberus/free", "cerberus/dispatch-code"}
    assert anonymous.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_credential_is_401_not_500(monkeypatch):
    app = create_app(identity_config(monkeypatch), http_transport=httpx.MockTransport(ok_upstream))
    response = await post(
        app, {"model": "cerberus/free", "messages": []}, {b"x-api-key": "cb-recón".encode("latin-1")}
    )
    assert response.status_code == 401
