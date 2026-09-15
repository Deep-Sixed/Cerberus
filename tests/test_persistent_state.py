"""Session 3 acceptance tests — cooldowns survive restart; scope escalation policy."""

import threading
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


def test_competing_connections_never_shorten_cooldown(tmp_path):
    db_file = tmp_path / "shared_cooldowns.db"
    store_a = SqliteCooldownStore(db_file)
    store_b = SqliteCooldownStore(db_file)

    now = 1_000_000.0
    # 1. Connection A applies a longer cooldown
    long_cooldown = store_a.apply(
        scope="model",
        provider="provider-x",
        credential="cred-1",
        model="model-a",
        reason="long_outage",
        duration_seconds=600,
        now=now,
    )
    assert long_cooldown.retry_at == now + 600
    assert long_cooldown.scope == "model"
    assert long_cooldown.reason == "long_outage"

    # 2. Connection B subsequently proposes a shorter cooldown
    short_proposed = store_b.apply(
        scope="model",
        provider="provider-x",
        credential="cred-1",
        model="model-a",
        reason="short_glitch",
        duration_seconds=30,
        now=now,
    )

    # 3. The persisted cooldown remains the longer one
    active_b = store_b.active_for("provider-x", "cred-1", "model-a", now=now)
    assert active_b is not None
    assert active_b.retry_at == now + 600

    # 4. Connection B's apply() return value reports the longer winning deadline
    assert short_proposed.retry_at == now + 600

    # 5. Scope and reason correspond to the winning row
    assert active_b.scope == "model"
    assert active_b.reason == "long_outage"
    assert short_proposed.scope == "model"
    assert short_proposed.reason == "long_outage"

    store_a.close()
    store_b.close()


def test_concurrent_threads_competing_cooldowns(tmp_path):
    db_file = tmp_path / "concurrent_cooldowns.db"
    store_a = SqliteCooldownStore(db_file)
    store_b = SqliteCooldownStore(db_file)

    now = 1_000_000.0
    barrier = threading.Barrier(2)
    results = {}

    def run_a():
        barrier.wait()
        results["a"] = store_a.apply(
            scope="model",
            provider="provider-y",
            credential="cred-1",
            model="model-b",
            reason="longer_timeout",
            duration_seconds=300,
            now=now,
        )

    def run_b():
        barrier.wait()
        results["b"] = store_b.apply(
            scope="model",
            provider="provider-y",
            credential="cred-1",
            model="model-b",
            reason="shorter_timeout",
            duration_seconds=60,
            now=now,
        )

    t_a = threading.Thread(target=run_a)
    t_b = threading.Thread(target=run_b)
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    # The maximum retry_at wins, and both stores see the winning persisted record
    active_a = store_a.active_for("provider-y", "cred-1", "model-b", now=now)
    active_b = store_b.active_for("provider-y", "cred-1", "model-b", now=now)
    assert active_a is not None and active_b is not None
    assert active_a.retry_at == now + 300
    assert active_b.retry_at == now + 300
    assert active_a.reason == "longer_timeout"
    assert active_b.reason == "longer_timeout"

    store_a.close()
    store_b.close()


def test_conditional_expiry_cleanup_does_not_delete_refreshed_cooldown(tmp_path):
    db_file = tmp_path / "expiry_cooldowns.db"
    store_a = SqliteCooldownStore(db_file)
    store_b = SqliteCooldownStore(db_file)

    t0 = 1_000_000.0
    # 1. Initial cooldown expiring at t0 + 10
    store_a.apply(
        scope="model",
        provider="provider-z",
        credential="cred-1",
        model="model-c",
        reason="initial",
        duration_seconds=10,
        now=t0,
    )

    t_expired = t0 + 20  # now expired
    # Connection A observes an expired row
    with store_a._lock:
        row = store_a._connection.execute(
            "SELECT scope, reason, retry_at FROM cooldowns WHERE provider=? AND credential=? AND model=?",
            ("provider-z", "cred-1", "model-c"),
        ).fetchone()
        assert row is not None
        assert row[2] <= t_expired

    # 2. Connection B refreshes that same cooldown
    refreshed = store_b.apply(
        scope="model",
        provider="provider-z",
        credential="cred-1",
        model="model-c",
        reason="refreshed_outage",
        duration_seconds=300,
        now=t_expired,
    )
    assert refreshed.retry_at == t_expired + 300

    # 3. Connection A runs its conditional cleanup for the observed expired timestamp
    with store_a._lock:
        store_a._connection.execute(
            "DELETE FROM cooldowns WHERE provider=? AND credential=? AND model=? AND retry_at <= ?",
            ("provider-z", "cred-1", "model-c", t_expired),
        )
        store_a._connection.commit()

    # 4. A's conditional cleanup cannot remove the refreshed row; subsequent lookup returns the refreshed cooldown
    active = store_a.active_for("provider-z", "cred-1", "model-c", now=t_expired + 5)
    assert active is not None
    assert active.retry_at == t_expired + 300
    assert active.reason == "refreshed_outage"

    store_a.close()
    store_b.close()
