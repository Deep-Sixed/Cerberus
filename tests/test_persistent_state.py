"""Session 3 acceptance tests — cooldowns survive restart; scope escalation policy."""

import time

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import CerberusConfig
from cerberus.state import SqliteCooldownStore


def store_at(tmp_path) -> SqliteCooldownStore:
    return SqliteCooldownStore(tmp_path / "state" / "cooldowns.db")


def test_cooldown_survives_restart(tmp_path):
    first = store_at(tmp_path)
    first.apply(
        scope="model", provider="google", credential="free-1", model="gemini-flash",
        reason="daily_quota_exhausted", duration_seconds=3600,
    )
    first.close()

    reopened = store_at(tmp_path)
    active = reopened.active_for("google", "free-1", "gemini-flash")
    assert active is not None
    assert active.reason == "daily_quota_exhausted"
    assert reopened.active_for("google", "free-1", "other-model") is None


def test_expired_cooldown_not_resurrected_after_restart(tmp_path):
    first = store_at(tmp_path)
    first.apply(
        scope="model", provider="google", credential="free-1", model="gemini-flash",
        reason="quota_429", duration_seconds=60, now=time.time() - 120,
    )
    first.close()
    reopened = store_at(tmp_path)
    assert reopened.active_for("google", "free-1", "gemini-flash") is None


def test_scopes_behave_like_memory_store(tmp_path):
    store = store_at(tmp_path)
    store.apply(scope="credential", provider="alpha", credential="a1", model=None, reason="account_rate_limit", duration_seconds=60)
    assert store.active_for("alpha", "a1", "any-model") is not None
    assert store.active_for("alpha", "a2", "any-model") is None
    store.apply(scope="provider", provider="beta", credential=None, model=None, reason="outage", duration_seconds=60)
    assert store.active_for("beta", "whatever", "anything") is not None


def test_never_shorten_rule_persists(tmp_path):
    store = store_at(tmp_path)
    long = store.apply(scope="model", provider="p", credential="c", model="m", reason="quota_429", duration_seconds=600)
    short = store.apply(scope="model", provider="p", credential="c", model="m", reason="quota_429", duration_seconds=10)
    assert short.retry_at == long.retry_at


def test_snapshot_matches_memory_semantics(tmp_path):
    store = store_at(tmp_path)
    store.apply(scope="model", provider="p", credential="c", model="m", reason="quota_429", duration_seconds=60)
    snapshot = store.snapshot()
    assert len(snapshot) == 1 and snapshot[0]["scope"] == "model"
    assert 0 < snapshot[0]["seconds_remaining"] <= 60


def escalation_config(monkeypatch, tmp_path) -> CerberusConfig:
    monkeypatch.setenv("GAMMA_KEY", "gamma-secret")
    return CerberusConfig.model_validate(
        {
            "metadata": {"version": "cerberus-2026-07-16.1"},
            "state": {"path": str(tmp_path / "cooldowns.db")},
            "providers": {
                "gamma": {
                    "base_url": "https://gamma.example/v1",
                    "credentials": {"main": {"api_key_env": "GAMMA_KEY"}},
                    "models": {"g-one": {"cost_tier": "free"}, "g-two": {"cost_tier": "free"}},
                    "quota_scope": "credential",
                },
            },
            "aliases": {
                "cerberus/free": {
                    "mode": "free",
                    "candidates": [
                        {"provider": "gamma", "credential": "main", "model": "g-one"},
                        {"provider": "gamma", "credential": "main", "model": "g-two"},
                    ],
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_credential_scoped_429_disables_sibling_models(monkeypatch, tmp_path):
    """quota_scope: credential -> one 429 excludes every model under that credential."""
    calls: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        body = httpx.Response(200, content=request.content).json()
        calls.append(body["model"])
        return httpx.Response(429)

    app = create_app(escalation_config(monkeypatch, tmp_path), http_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})
            health = await client.get("/health")

    assert response.status_code == 503
    assert calls == ["g-one"]  # g-two never attempted: credential-wide cooldown applied
    cooldowns = health.json()["cooldowns"]
    assert cooldowns[0]["scope"] == "credential" and cooldowns[0]["credential"] == "main"


@pytest.mark.asyncio
async def test_cooldown_survives_app_restart(monkeypatch, tmp_path):
    """The S3 headline acceptance test: recorded exhaustion excludes the target after restart."""

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    config = escalation_config(monkeypatch, tmp_path)
    app_one = create_app(config, http_transport=httpx.MockTransport(upstream))
    async with app_one.router.lifespan_context(app_one):
        transport = httpx.ASGITransport(app=app_one)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})

    upstream_calls = 0

    async def upstream_after_restart(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"choices": []})

    app_two = create_app(config, http_transport=httpx.MockTransport(upstream_after_restart))
    async with app_two.router.lifespan_context(app_two):
        transport = httpx.ASGITransport(app=app_two)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})

    assert response.status_code == 503  # still cooled from before the restart
    assert upstream_calls == 0
