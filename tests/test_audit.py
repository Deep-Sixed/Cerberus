"""GET /admin/audit — persisted control-plane lifecycle history.

Read-only by construction: this endpoint reads, and nothing writes, deletes or
acknowledges a record. It is not routing history — that is the ephemeral ring at
/admin/events — and these tests hold that boundary as well as the contract.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import yaml

from cerberus.app import create_app
from cerberus.registry.loader import load_config_document

CSRF = {"x-cerberus-csrf": "1"}
LOOPBACK = ("127.0.0.1", 40001)
REMOTE = ("203.0.113.9", 40001)
SECRET = "alpha-secret-disclosure-probe"


def raw_config(tmp_path, version="cerberus-2026-09-17.1", *, state=True) -> dict:
    raw: dict = {
        "metadata": {"version": version},
        "telemetry": {},
        "providers": {
            "alpha": {
                "base_url": "https://alpha.test/v1",
                "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                "models": {"a-free": {"cost_tier": "free"}},
            }
        },
        "aliases": {
            "cerberus/free": {
                "mode": "free",
                "candidates": [{"provider": "alpha", "credential": "main", "model": "a-free"}],
            }
        },
    }
    if state:
        raw["state"] = {"path": str(tmp_path / "state.sqlite3")}
    return raw


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", SECRET)


def write(tmp_path, raw, name="config.yaml") -> str:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


def build(path):
    return create_app(load_config_document(path))


def client_for(app, addr=LOOPBACK):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=addr), base_url="http://test"
    )


async def audit_of(client, query="") -> dict:
    response = await client.get("/admin/audit" + query)
    assert response.status_code == 200, response.text
    return response.json()


# -- the records themselves ---------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_and_registration_appear(env, tmp_path):
    app = build(write(tmp_path, raw_config(tmp_path)))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            payload = await audit_of(client)

    assert payload["persistent"] is True
    actions = [r["action"] for r in payload["records"]]
    assert "bootstrap" in actions
    assert "revision_registered" in actions
    for record in payload["records"]:
        assert record["revision"] == "cerberus-2026-09-17.1"
        assert record["checksum"].startswith("sha256:")


@pytest.mark.asyncio
async def test_explicit_activation_appears_newest_first(env, tmp_path):
    path = write(tmp_path, raw_config(tmp_path))
    app = build(path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            before = await audit_of(client)
            assert "activate" not in [r["action"] for r in before["records"]]

            assert (await client.post("/admin/activate", json={"path": path}, headers=CSRF)).status_code == 200
            after = await audit_of(client)

    assert after["records"][0]["action"] == "activate"  # newest first
    ids = [r["id"] for r in after["records"]]
    assert ids == sorted(ids, reverse=True)


@pytest.mark.asyncio
async def test_detail_json_is_returned_as_an_object_never_raw(env, tmp_path):
    path = write(tmp_path, raw_config(tmp_path))
    app = build(path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await client.post("/admin/activate", json={"path": path}, headers=CSRF)
            response = await client.get("/admin/audit")

    payload = response.json()
    activate = next(r for r in payload["records"] if r["action"] == "activate")
    assert isinstance(activate["detail"], dict)
    assert "activated_at" in activate["detail"]
    registered = next(r for r in payload["records"] if r["action"] == "revision_registered")
    assert registered["detail"] == {}
    # the stored column is this module's business and is never handed over
    assert "detail_json" not in response.text
    assert set(payload["records"][0]) == {"id", "occurred_at", "action", "revision", "checksum", "detail"}


# -- the bounded read ---------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_defaults_clamps_and_caps(env, tmp_path):
    path = write(tmp_path, raw_config(tmp_path))
    app = build(path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            # a handful of extra activations so a limit can bite
            for _ in range(4):
                await client.post("/admin/activate", json={"path": path}, headers=CSRF)

            assert len((await audit_of(client, "?limit=2"))["records"]) == 2
            assert len((await audit_of(client, "?limit=1"))["records"]) == 1
            # below the minimum clamps up, not to zero or an error
            assert len((await audit_of(client, "?limit=0"))["records"]) == 1
            assert len((await audit_of(client, "?limit=-5"))["records"]) == 1
            # unreadable takes the default rather than failing the read
            default_count = len((await audit_of(client))["records"])
            assert len((await audit_of(client, "?limit=abc"))["records"]) == default_count
            # above the maximum is capped; the table is smaller than the cap here,
            # so the proof is that it answers rather than refusing
            assert len((await audit_of(client, "?limit=100000"))["records"]) == default_count


# -- persistence --------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_survive_restart(env, tmp_path):
    path = write(tmp_path, raw_config(tmp_path))
    app = build(path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await client.post("/admin/activate", json={"path": path}, headers=CSRF)
            before = await audit_of(client)

    restarted = build(path)  # a second process over the same state database
    async with restarted.router.lifespan_context(restarted):
        async with client_for(restarted) as client:
            after = await audit_of(client)

    assert after["persistent"] is True
    assert [r["id"] for r in after["records"]] [:len(before["records"])] == [r["id"] for r in before["records"]]
    assert "activate" in [r["action"] for r in after["records"]]


@pytest.mark.asyncio
async def test_memory_mode_reports_no_durable_history(env, tmp_path):
    app = build(write(tmp_path, raw_config(tmp_path, state=False)))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            payload = await audit_of(client)

    assert payload == {"persistent": False, "records": []}


# -- boundary -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_is_behind_the_read_only_admin_boundary(env, tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_ADMIN_TOKEN", "admin-token-value")
    monkeypatch.setenv("CERBERUS_API_TOKEN", "inference-token-value")
    raw = raw_config(tmp_path)
    raw["server"] = {
        "host": "0.0.0.0",
        "port": 4000,
        "api_token_env": "CERBERUS_API_TOKEN",
        "admin_token_env": "CERBERUS_ADMIN_TOKEN",
    }
    app = build(write(tmp_path, raw))
    async with app.router.lifespan_context(app):
        async with client_for(app, REMOTE) as remote:
            anonymous = await remote.get("/admin/audit")
            credentialed = await remote.get("/admin/audit", headers={"authorization": "Bearer admin-token-value"})
            # the inference token never opens the admin surface
            inference = await remote.get("/admin/audit", headers={"authorization": "Bearer inference-token-value"})
        async with client_for(app, LOOPBACK) as local:
            loopback = await local.get("/admin/audit")

    # with an admin credential configured, an anonymous remote peer is told to
    # authenticate rather than simply refused
    assert anonymous.status_code == 401
    assert credentialed.status_code == 200
    assert inference.status_code in (401, 403)
    assert loopback.status_code == 200


@pytest.mark.asyncio
async def test_audit_offers_no_mutation(env, tmp_path):
    app = build(write(tmp_path, raw_config(tmp_path)))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                response = await client.request(method, "/admin/audit", headers=CSRF)
                assert response.status_code == 405, f"{method} -> {response.status_code}"


@pytest.mark.asyncio
async def test_no_secret_reaches_the_audit_surface(env, tmp_path):
    """Every field is a closed literal, a schema-constrained version, a digest,
    a timestamp or {} — proven here against the serialized response."""

    path = write(tmp_path, raw_config(tmp_path))
    app = build(path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await client.post("/admin/activate", json={"path": path}, headers=CSRF)
            body = (await client.get("/admin/audit")).text

    assert SECRET not in body
    assert "ALPHA_KEY" not in body          # not even the variable's name
    assert "api_key" not in body
    assert str(tmp_path) not in body         # no filesystem path
    assert "Bearer" not in body

    # and the stored table itself holds only the four actions _audit is called with
    con = sqlite3.connect(tmp_path / "state.sqlite3")
    actions = {row[0] for row in con.execute("SELECT DISTINCT action FROM audit_records")}
    details = {row[0] for row in con.execute("SELECT DISTINCT detail_json FROM audit_records")}
    con.close()
    assert actions <= {"bootstrap", "activate", "rollback", "revision_registered"}
    for raw_detail in details:
        parsed = json.loads(raw_detail)
        assert set(parsed) <= {"activated_at"}, parsed


@pytest.mark.asyncio
async def test_audit_is_not_routing_history(env, tmp_path):
    """Both carry timestamps; they are different surfaces and stay that way."""

    path = write(tmp_path, raw_config(tmp_path))
    app = build(path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            audit = await audit_of(client)
            events = (await client.get("/admin/events")).json()

    assert events["events"] == []           # nothing routed
    assert audit["records"]                  # yet lifecycle history exists
    for record in audit["records"]:
        for routing_only in ("outcome", "attempts", "exclusions", "latency_ms", "request_id", "alias"):
            assert routing_only not in record
