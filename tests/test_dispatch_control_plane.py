"""Cerberus Dispatch SQLite control-plane and operational-state contract."""

from __future__ import annotations

import json
import sqlite3
import time

import httpx
import pytest
import yaml

from cerberus.app import create_app
from cerberus.control import ConfigLifecycle, SqliteControlPlane
from cerberus.control.sqlite import CONTROL_SCHEMA_VERSION
from cerberus.registry.loader import ConfigDocument
from cerberus.registry.loader import load_config_document
from cerberus.registry.schema import CerberusConfig


def raw_config(version: str, state_path: str, *, primary_model: str = "fast") -> dict:
    return {
        "metadata": {"version": version},
        "state": {"path": state_path},
        "providers": {
            "local": {
                "base_url": "http://local.test/v1",
                "credentials": {"main": {"api_key_env": "LOCAL_API_KEY"}},
                "models": {
                    primary_model: {"cost_tier": "free", "capabilities": ["coding"]},
                    "review": {"cost_tier": "free", "capabilities": ["coding"]},
                },
            }
        },
        "aliases": {
            "cerberus/coding-fast": {
                "mode": "dispatch",
                "candidates": [{"provider": "local", "credential": "main", "model": primary_model}],
            },
            "cerberus/fusion-review": {
                "mode": "fusion",
                "candidates": [
                    {"provider": "local", "credential": "main", "model": primary_model},
                    {"provider": "local", "credential": "main", "model": "review"},
                ],
                "fusion": {
                    "max_panel_members": 2,
                    "timeout_seconds": 30,
                    "judge": {"provider": "local", "credential": "main", "model": "review"},
                },
            },
        },
        "identities": {
            "ide": {
                "credential_env": "IDE_API_KEY",
                "allowed_modes": ["dispatch", "fusion"],
                "allowed_aliases": ["cerberus/coding-fast", "cerberus/fusion-review"],
            }
        },
    }


def document(raw: dict) -> ConfigDocument:
    config = CerberusConfig.model_validate(raw)
    return ConfigDocument(
        config=config,
        version=config.metadata.version,
        checksum=f"sha256:{config.metadata.version}",
        source_path="<test>",
    )


def write_document(tmp_path, name: str, raw: dict) -> ConfigDocument:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config_document(path)


def test_revision_materializes_dispatch_catalog_without_secret_values(monkeypatch, tmp_path):
    state_path = tmp_path / "cerberus.sqlite3"
    monkeypatch.setenv("LOCAL_API_KEY", "provider-secret-must-not-persist")
    monkeypatch.setenv("IDE_API_KEY", "identity-secret-must-not-persist")
    control = SqliteControlPlane(state_path)
    active = control.bootstrap(document(raw_config("cerberus-2026-09-14.1", str(state_path))))
    control.close()

    assert active.version == "cerberus-2026-09-14.1"
    connection = sqlite3.connect(state_path)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "service_catalog", "service_aliases", "alias_bindings", "route_paths", "route_members",
        "identity_alias_policy", "provider_credentials_ref", "config_revisions",
        "control_activation", "provider_health", "routing_events", "usage_records", "audit_records",
    } <= tables
    assert connection.execute("SELECT count(*) FROM service_aliases").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM service_catalog").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM route_paths").fetchone()[0] == 3
    assert connection.execute("SELECT role FROM route_members ORDER BY role").fetchall() == [
        ("analyst",), ("outer",), ("panel",), ("panel",),
    ]
    assert connection.execute("SELECT secret_env_ref FROM provider_credentials_ref").fetchone()[0] == "LOCAL_API_KEY"
    connection.close()
    database_bytes = state_path.read_bytes()
    assert b"provider-secret-must-not-persist" not in database_bytes
    assert b"identity-secret-must-not-persist" not in database_bytes


def test_active_revision_restores_after_restart(monkeypatch, tmp_path):
    state_path = tmp_path / "cerberus.sqlite3"
    monkeypatch.setenv("LOCAL_API_KEY", "provider-secret")
    monkeypatch.setenv("IDE_API_KEY", "identity-secret")
    v1 = document(raw_config("cerberus-2026-09-14.1", str(state_path), primary_model="fast"))
    v2 = document(raw_config("cerberus-2026-09-14.2", str(state_path), primary_model="review"))

    first = SqliteControlPlane(state_path)
    ConfigLifecycle(v1, repository=first)
    first.register(v2)
    first.activate(v2)
    first.close()

    reopened = SqliteControlPlane(state_path)
    restored = reopened.bootstrap(v1)
    assert restored.version == v2.version
    assert restored.config.aliases["cerberus/coding-fast"].candidates[0].model == "review"
    reopened.close()


def test_unknown_control_schema_version_fails_closed(tmp_path):
    """A database written by a NEWER Cerberus is refused, never silently opened.

    The sentinel tracks CONTROL_SCHEMA_VERSION rather than naming a literal, so
    a future schema bump cannot quietly turn this case into a supported one (as
    it did when version 2 was introduced).
    """

    unsupported = CONTROL_SCHEMA_VERSION + 1
    state_path = tmp_path / "cerberus.sqlite3"
    control = SqliteControlPlane(state_path)
    control.close()
    connection = sqlite3.connect(state_path)
    connection.execute("UPDATE control_schema SET version=? WHERE singleton=1", (unsupported,))
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match=f"unsupported Cerberus control schema version {unsupported}"):
        SqliteControlPlane(state_path)


def test_persisted_revision_cannot_move_its_control_database(monkeypatch, tmp_path):
    state_path = tmp_path / "cerberus.sqlite3"
    monkeypatch.setenv("LOCAL_API_KEY", "provider-secret")
    monkeypatch.setenv("IDE_API_KEY", "identity-secret")
    control = SqliteControlPlane(state_path)
    control.bootstrap(document(raw_config("cerberus-2026-09-14.1", str(state_path))))
    moved = document(
        raw_config("cerberus-2026-09-14.2", str(tmp_path / "different.sqlite3"))
    )

    with pytest.raises(ValueError, match="state.path is a boot-time control-plane locator"):
        control.register(moved)
    control.close()


@pytest.mark.asyncio
async def test_app_activation_restores_active_snapshot_after_restart(monkeypatch, tmp_path):
    state_path = tmp_path / "cerberus.sqlite3"
    monkeypatch.setenv("LOCAL_API_KEY", "provider-secret")
    monkeypatch.setenv("IDE_API_KEY", "identity-secret")
    v1 = write_document(
        tmp_path,
        "v1.yaml",
        raw_config("cerberus-2026-09-14.1", str(state_path), primary_model="fast"),
    )
    v2 = write_document(
        tmp_path,
        "v2.yaml",
        raw_config("cerberus-2026-09-14.2", str(state_path), primary_model="review"),
    )

    first = create_app(v1, http_transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    async with first.router.lifespan_context(first):
        first.state.lifecycle.activate(v2.source_path)

    restarted = create_app(v1, http_transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    assert restarted.state.lifecycle.active.version == v2.version
    assert restarted.state.lifecycle.active.checksum == v2.checksum
    assert restarted.state.lifecycle.active.config.aliases["cerberus/coding-fast"].candidates[0].model == "review"
    async with restarted.router.lifespan_context(restarted):
        pass


@pytest.mark.asyncio
async def test_health_can_remove_configured_path_and_events_persist(monkeypatch, tmp_path):
    state_path = tmp_path / "cerberus.sqlite3"
    monkeypatch.setenv("LOCAL_API_KEY", "provider-secret")
    monkeypatch.setenv("IDE_API_KEY", "identity-secret")
    doc = document(raw_config("cerberus-2026-09-14.1", str(state_path)))
    calls: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3, "cost": 0.001},
            },
        )

    app = create_app(doc, http_transport=httpx.MockTransport(upstream))
    app.state.control_plane.set_provider_health("unconfigured-provider", "healthy")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"authorization": "Bearer identity-secret"}
            body = {"model": "cerberus/coding-fast", "messages": [{"role": "user", "content": "code"}]}
            ok = await client.post("/v1/chat/completions", json=body, headers=headers)
            app.state.control_plane.set_provider_health("local", "down")
            unavailable = await client.post("/v1/chat/completions", json=body, headers=headers)

    assert ok.status_code == 200
    assert unavailable.status_code == 503
    assert len(calls) == 1
    connection = sqlite3.connect(state_path)
    payloads = [json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM routing_events")]
    usage = connection.execute("SELECT usage_json, monetary_cost FROM usage_records WHERE usage_json IS NOT NULL").fetchone()
    connection.close()
    assert {payload["outcome"] for payload in payloads} == {"success", "routing_exhausted"}
    success = next(payload for payload in payloads if payload["outcome"] == "success")
    assert success["identity"] == "ide"
    assert success["config_version"] == doc.version
    assert success["config_checksum"] == doc.checksum
    assert success["credential_ref"] == "local/main"
    assert usage == ('{"completion_tokens": 1, "prompt_tokens": 2, "total_tokens": 3}', 0.001)


# -- provider health recovery (PR #7) -------------------------------------
# `down` is a coarse, provider-wide exclusion. Before schema v2 it had no expiry:
# it survived restart and was cleared only by a later successful manual probe, so
# a single transient failure could hold a provider out of routing indefinitely.


def test_down_expires_and_restores_the_provider(tmp_path):
    control = SqliteControlPlane(tmp_path / "cerberus.sqlite3")
    try:
        control.set_provider_health("alpha", "down", ttl_seconds=60)
        assert control.provider_available("alpha") is False
        # still excluded inside the window, available once it passes
        assert control.provider_available("alpha", now=time.time() + 59) is False
        assert control.provider_available("alpha", now=time.time() + 61) is True
    finally:
        control.close()


def test_down_expiry_survives_restart_and_still_expires(tmp_path):
    state_path = tmp_path / "cerberus.sqlite3"
    control = SqliteControlPlane(state_path)
    control.set_provider_health("alpha", "down", ttl_seconds=60)
    control.close()

    # a restart reloads the verdict - and its expiry, so it cannot outlive it
    reopened = SqliteControlPlane(state_path)
    try:
        assert reopened.provider_available("alpha") is False
        assert reopened.provider_available("alpha", now=time.time() + 61) is True
    finally:
        reopened.close()


def test_healthy_and_degraded_never_exclude(tmp_path):
    control = SqliteControlPlane(tmp_path / "cerberus.sqlite3")
    try:
        for status in ("unknown", "healthy", "degraded"):
            control.set_provider_health("alpha", status)
            assert control.provider_available("alpha") is True
            row = control._connection.execute(
                "SELECT expires_at FROM provider_health WHERE provider='alpha'"
            ).fetchone()
            assert row["expires_at"] is None, f"{status} must carry no expiry"
    finally:
        control.close()


def test_v1_permanent_down_rows_are_retired_on_upgrade(tmp_path):
    """A pre-v2 database's permanent `down` must not survive the upgrade."""

    state_path = tmp_path / "cerberus.sqlite3"
    control = SqliteControlPlane(state_path)
    control.close()

    # rewind to the v1 shape: no expires_at column, version 1, a permanent down
    connection = sqlite3.connect(state_path)
    connection.execute("ALTER TABLE provider_health DROP COLUMN expires_at")
    connection.execute(
        "INSERT INTO provider_health (provider, status, checked_at, latency_ms, detail)"
        " VALUES ('alpha', 'down', '2026-01-01T00:00:00+00:00', NULL, 'ConnectTimeout')"
    )
    connection.execute("UPDATE control_schema SET version=1 WHERE singleton=1")
    connection.commit()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_health)")}
    assert "expires_at" not in columns, "precondition: this is a v1-shaped database"
    connection.close()

    upgraded = SqliteControlPlane(state_path)
    try:
        assert upgraded.provider_available("alpha") is True, "legacy down must not survive upgrade"
        stored = upgraded._connection.execute(
            "SELECT status, detail, expires_at FROM provider_health WHERE provider='alpha'"
        ).fetchone()
        # retired, not erased: the recorded verdict stays visible for the operator
        assert stored["status"] == "down" and stored["detail"] == "ConnectTimeout"
        assert stored["expires_at"] is not None and stored["expires_at"] <= time.time()
        version = upgraded._connection.execute(
            "SELECT version FROM control_schema WHERE singleton=1"
        ).fetchone()[0]
        assert version == CONTROL_SCHEMA_VERSION
    finally:
        upgraded.close()


def test_down_without_expiry_is_never_permanent(tmp_path):
    """Defence in depth: a `down` carrying no expiry at all excludes nothing."""

    state_path = tmp_path / "cerberus.sqlite3"
    control = SqliteControlPlane(state_path)
    try:
        control._connection.execute(
            "INSERT INTO provider_health (provider, status, checked_at, expires_at)"
            " VALUES ('alpha', 'down', '2026-01-01T00:00:00+00:00', NULL)"
        )
        control._connection.commit()
        control._health = control._load_health()
        assert control.provider_available("alpha") is True
    finally:
        control.close()


@pytest.mark.asyncio
async def test_probe_transport_failure_records_degraded_not_down(monkeypatch, tmp_path):
    """A transient probe failure must not remove the provider from routing.

    Previously an httpx error wrote `down`, which excluded every route through
    that provider until an operator ran another successful probe - a harsher
    verdict than the `degraded` a 5xx earns, from a strictly weaker signal.
    """

    state_path = tmp_path / "cerberus.sqlite3"
    monkeypatch.setenv("LOCAL_API_KEY", "provider-secret")
    monkeypatch.setenv("IDE_API_KEY", "identity-secret")
    doc = write_document(tmp_path, "v1.yaml", raw_config("cerberus-2026-09-14.1", str(state_path)))

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            raise httpx.ConnectTimeout("probe timed out", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(doc, http_transport=httpx.MockTransport(flaky))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40001))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            probe = await client.post(
                "/admin/providers/local/test", headers={"x-cerberus-csrf": "1"}
            )
            assert probe.status_code == 200
            assert probe.json() == {"ok": False, "reason": "ConnectTimeout"}

            control_plane = app.state.control_plane
            status = control_plane._connection.execute(
                "SELECT status, expires_at FROM provider_health WHERE provider='local'"
            ).fetchone()
            assert status["status"] == "degraded"
            assert status["expires_at"] is None
            assert control_plane.provider_available("local") is True

            # and routing still works through the provider the probe could not reach
            routed = await client.post(
                "/v1/chat/completions",
                headers={"authorization": "Bearer identity-secret"},
                json={"model": "cerberus/coding-fast", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert routed.status_code == 200
