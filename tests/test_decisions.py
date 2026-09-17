"""Operational decision visibility — what /admin/events gives the Decisions UI.

The console renders recorded history and nothing else: an event's attempts are
the attempts, its exclusions are the exclusions, and its revision and checksum
are the ones that were active when it ran. These tests drive real requests
through the gateway and assert the admin read surface carries what each screen
needs, in the vocabularies Cerberus actually emits.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import yaml

from cerberus.app import create_app
from cerberus.registry.loader import load_config_document
from tests.test_streaming_invariants import TrackedByteStream

BEARER_WIDE = {"authorization": "Bearer wide-secret-value"}
BEARER_NARROW = {"authorization": "Bearer narrow-secret-value"}


def base_raw(tmp_path, *, state: bool = True) -> dict:
    raw: dict = {
        "metadata": {"version": "cerberus-2026-09-17.1"},
        "telemetry": {},
        "providers": {
            "alpha": {
                "base_url": "https://alpha.test/v1",
                "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                "models": {"a-free": {"cost_tier": "free"}},
            },
            "beta": {
                "base_url": "https://beta.test/v1",
                "credentials": {"main": {"api_key_env": "BETA_KEY"}},
                "models": {"b-free": {"cost_tier": "free"}},
            },
        },
        "aliases": {
            "cerberus/primary": {
                "mode": "dispatch",
                "candidates": [
                    {"provider": "alpha", "credential": "main", "model": "a-free"},
                    {"provider": "beta", "credential": "main", "model": "b-free"},
                ],
            },
            "cerberus/other": {
                "mode": "dispatch",
                "candidates": [{"provider": "beta", "credential": "main", "model": "b-free"}],
            },
            "cerberus/fusion-review": {
                "mode": "fusion",
                "candidates": [{"provider": "alpha", "credential": "main", "model": "a-free"}],
                "fusion": {
                    "max_panel_members": 1,
                    "timeout_seconds": 30,
                    "judge": {"provider": "alpha", "credential": "main", "model": "a-free"},
                },
            },
        },
        "identities": {
            "wide": {
                "credential_env": "WIDE_KEY",
                "allowed_modes": ["dispatch", "fusion"],
                "allowed_aliases": ["cerberus/primary", "cerberus/other", "cerberus/fusion-review"],
            },
            "narrow": {
                "credential_env": "NARROW_KEY",
                "allowed_modes": ["dispatch"],
                "allowed_aliases": ["cerberus/primary"],
            },
        },
    }
    if state:
        raw["state"] = {"path": str(tmp_path / "state.sqlite3")}
    return raw


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret-value")
    monkeypatch.setenv("BETA_KEY", "beta-secret-value")
    monkeypatch.setenv("WIDE_KEY", "wide-secret-value")
    monkeypatch.setenv("NARROW_KEY", "narrow-secret-value")


def build(tmp_path, raw, *, transport, telemetry_transport=None, name="config.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return create_app(
        load_config_document(str(path)),
        http_transport=transport,
        telemetry_transport=telemetry_transport,
    )


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://test"
    )


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})


async def route(client, alias="cerberus/primary", headers=None, **extra):
    body = {"model": alias, "messages": [{"role": "user", "content": "hi"}], **extra}
    return await client.post("/v1/chat/completions", json=body, headers=headers or BEARER_WIDE)


async def events_of(client) -> list[dict]:
    response = await client.get("/admin/events")
    assert response.status_code == 200, response.text
    return response.json()["events"]


# -- the ring ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_ring_carries_no_decisions(env, tmp_path):
    """The console's empty state is driven by an empty list, not a flag."""

    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            assert await events_of(client) == []


@pytest.mark.asyncio
async def test_successful_direct_route(env, tmp_path):
    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            assert (await route(client)).status_code == 200
            event = (await events_of(client))[0]

    assert event["outcome"] == "success"
    assert event["http_status"] == 200
    assert event["provider"] == "alpha" and event["model"] == "a-free"
    assert event["credential_ref"] == "alpha/main"
    assert event["used_fallback"] is False
    assert event["streaming"] is False
    assert event["identity"] == "wide"
    assert event["attempt_count"] == 1
    assert event["attempts"][0]["outcome"] == "response"
    assert event["config_version"] == "cerberus-2026-09-17.1"
    assert event["config_checksum"].startswith("sha256:")
    assert isinstance(event["latency_ms"], (int, float))


@pytest.mark.asyncio
async def test_fallback_records_both_attempts_in_order(env, tmp_path):
    seen: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "alpha" in str(request.url):
            return httpx.Response(503, json={"error": "down"})
        return ok(request)

    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            assert (await route(client)).status_code == 200
            event = (await events_of(client))[0]

    assert event["outcome"] == "success"
    assert event["used_fallback"] is True
    assert event["attempt_count"] == 2
    assert [a["outcome"] for a in event["attempts"]] == ["retryable_status", "response"]
    assert event["attempts"][0]["used_fallback"] is False
    assert event["attempts"][1]["used_fallback"] is True
    assert event["provider"] == "beta"  # the one that answered


@pytest.mark.asyncio
async def test_routing_exhausted_has_exclusions_and_no_selected_provider(env, tmp_path):
    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            app.state.control_plane.set_provider_health("alpha", "down", ttl_seconds=60)
            app.state.control_plane.set_provider_health("beta", "down", ttl_seconds=60)
            response = await route(client)
            event = (await events_of(client))[0]

    assert response.status_code == 503
    assert event["outcome"] == "routing_exhausted"
    assert event["provider"] is None and event["model"] is None
    assert event["attempts"] == []  # nothing was attempted
    assert {x["reason"] for x in event["exclusions"]} == {"provider_down"}


@pytest.mark.asyncio
async def test_quota_attempt_carries_the_cooldown_scope_it_applied(env, tmp_path):
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "quota"})

    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await route(client)
            event = (await events_of(client))[0]

    quota = [a for a in event["attempts"] if a["http_status"] == 429]
    assert quota, event["attempts"]
    assert all(a["outcome"] == "retryable_status" for a in quota)
    assert quota[0]["cooldown_scope"] is not None


@pytest.mark.asyncio
async def test_streaming_interruption_is_recorded_as_such(env, tmp_path):
    stream = TrackedByteStream(
        [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'], fail_after=1
    )

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            with pytest.raises(RuntimeError, match="controlled mid-stream failure"):
                async with client.stream(
                    "POST", "/v1/chat/completions",
                    json={"model": "cerberus/primary", "messages": [], "stream": True},
                    headers=BEARER_WIDE,
                ) as response:
                    async for _ in response.aiter_bytes():
                        pass
            event = (await events_of(client))[0]

    assert event["outcome"] == "stream_interrupted"
    assert event["streaming"] is True
    assert event["attempts"][-1]["outcome"] == "stream_interrupted"


@pytest.mark.asyncio
async def test_unauthorized_decision_is_recorded_for_the_alias_it_was_refused_on(env, tmp_path):
    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await route(client, alias="cerberus/other", headers=BEARER_NARROW)
            event = (await events_of(client))[0]

    assert response.status_code == 403
    assert event["outcome"] == "unauthorized"
    assert event["alias"] == "cerberus/other"
    assert event["identity"] == "narrow"
    assert event["provider"] is None


@pytest.mark.asyncio
async def test_fusion_unavailable_is_recorded(env, tmp_path):
    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            # the judge's provider going down is one of fusion's own three gates
            app.state.control_plane.set_provider_health("alpha", "down", ttl_seconds=60)
            response = await route(client, alias="cerberus/fusion-review")
            event = (await events_of(client))[0]

    assert response.status_code == 503
    assert event["outcome"] == "fusion_unavailable"
    assert event["alias"] == "cerberus/fusion-review"


# -- history stays history ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_decision_keeps_its_own_revision_after_a_later_activation(env, tmp_path):
    """After an activation the console must still show the revision a decision
    ran under, not the one that is active now."""

    raw = base_raw(tmp_path)
    app = build(tmp_path, raw, transport=httpx.MockTransport(ok))

    later = dict(raw)
    later["metadata"] = {"version": "cerberus-2026-09-17.2"}
    later_path = tmp_path / "v2.yaml"
    later_path.write_text(yaml.safe_dump(later), encoding="utf-8")

    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await route(client)
            before = (await events_of(client))[0]

            app.state.lifecycle.activate(str(later_path))
            status = (await client.get("/admin/status")).json()
            after = (await events_of(client))[0]

    assert status["active"]["version"] == "cerberus-2026-09-17.2"  # the gateway moved on
    assert after["config_version"] == "cerberus-2026-09-17.1"  # the decision did not
    assert after["config_checksum"] == before["config_checksum"]


@pytest.mark.asyncio
async def test_filtering_one_alias_never_shows_another(env, tmp_path):
    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await route(client, alias="cerberus/primary")
            await route(client, alias="cerberus/other")
            await route(client, alias="cerberus/primary")
            ring = await events_of(client)

    # the ring is newest-first, as the console renders it
    assert [e["alias"] for e in ring] == ["cerberus/primary", "cerberus/other", "cerberus/primary"]
    primary = [e for e in ring if e["alias"] == "cerberus/primary"]
    assert len(primary) == 2
    assert all(e["alias"] == "cerberus/primary" for e in primary)


@pytest.mark.asyncio
async def test_current_route_state_cannot_rewrite_a_recorded_decision(env, tmp_path):
    """A cooldown that has since expired, or health that has since recovered,
    changes /admin/routes and must change nothing about recorded history."""

    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            app.state.control_plane.set_provider_health("alpha", "down", ttl_seconds=60)
            app.state.control_plane.set_provider_health("beta", "down", ttl_seconds=60)
            await route(client)
            recorded = json.dumps((await events_of(client))[0], sort_keys=True)

            # everything recovers: the projection now says both paths are fine
            app.state.control_plane.set_provider_health("alpha", "healthy")
            app.state.control_plane.set_provider_health("beta", "healthy")
            routes = (await client.get("/admin/routes")).json()
            after = json.dumps((await events_of(client))[0], sort_keys=True)

    primary = next(a for a in routes["aliases"] if a["alias"] == "cerberus/primary")
    assert any(p["state"] in {"eligible", "standby"} for p in primary["paths"])  # routes moved on
    assert after == recorded  # the decision did not


# -- telemetry delivery ------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_status_disabled_without_a_sink_or_store(env, tmp_path):
    app = build(tmp_path, base_raw(tmp_path, state=False), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            telemetry = (await client.get("/admin/health")).json()["telemetry"]

    assert telemetry["status"] == "disabled"


@pytest.mark.asyncio
async def test_delivery_status_pending_then_healthy_then_degraded(env, tmp_path):
    token = tmp_path / "token"
    token.write_text("telemetry-test-value", encoding="utf-8")
    raw = base_raw(tmp_path)
    raw["telemetry"] = {
        "endpoint": "http://sink.test/v1/telemetry/routing-records",
        "bearer_token_file": str(token),
        "timeout_seconds": 0.5,
    }
    accepting = {"ok": True}

    async def sink(_request: httpx.Request) -> httpx.Response:
        if accepting["ok"]:
            return httpx.Response(201, json={"status": "ok"})
        return httpx.Response(403, json={"error": "denied"})

    app = build(tmp_path, raw, transport=httpx.MockTransport(ok),
                telemetry_transport=httpx.MockTransport(sink))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            pending = (await client.get("/admin/health")).json()["telemetry"]
            assert pending["status"] == "pending"
            assert pending["last_success_at"] is None

            await route(client)
            healthy = None
            for _ in range(40):
                healthy = (await client.get("/admin/health")).json()["telemetry"]
                if healthy["status"] == "healthy":
                    break
                await asyncio.sleep(0)  # delivery is a background task
            assert healthy["status"] == "healthy", healthy
            assert healthy["last_success_at"] is not None

            accepting["ok"] = False
            await route(client)
            degraded = None
            for _ in range(40):
                degraded = (await client.get("/admin/health")).json()["telemetry"]
                if degraded["status"] == "degraded":
                    break
                await asyncio.sleep(0)

    assert degraded["status"] == "degraded", degraded
    assert degraded["consecutive_failures"] >= 1
    assert degraded["last_status_code"] == 403


@pytest.mark.asyncio
async def test_health_reports_dropped_events_and_invents_no_queue_state(env, tmp_path):
    app = build(tmp_path, base_raw(tmp_path), transport=httpx.MockTransport(ok))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            telemetry = (await client.get("/admin/health")).json()["telemetry"]

    assert telemetry["dropped_events"] == 0
    assert isinstance(telemetry["dropped_events"], int)
    # the contract carries delivery health and nothing about the queue itself or
    # where events go, so the console has nothing to draw a depth or sink from
    assert set(telemetry) == {
        "status", "last_error", "last_status_code", "consecutive_failures",
        "last_error_at", "last_success_at", "dropped_events",
    }
