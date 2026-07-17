"""Sessions 6-7 acceptance tests — config lifecycle (SPEC test 14) and shadow mode (SPEC test 9)."""

import json

import httpx
import pytest
import yaml

from cerberus.app import create_app
from cerberus.registry import load_config_document


def raw_config(version: str, model: str = "alpha-one", host: str = "alpha.example") -> dict:
    return {
        "metadata": {"version": version},
        "telemetry": {},
        "providers": {
            "alpha": {
                "base_url": f"https://{host}/v1",
                "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                "models": {model: {"cost_tier": "free"}},
            },
        },
        "aliases": {
            "cerberus/free": {
                "mode": "free",
                "candidates": [{"provider": "alpha", "credential": "main", "model": model}],
            },
        },
    }


def write_config(tmp_path, name: str, raw: dict) -> str:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


def with_telemetry(raw: dict, tmp_path) -> dict:
    token = tmp_path / "token"
    token.write_text("telemetry-token", encoding="utf-8")
    raw["telemetry"] = {
        "endpoint": "http://contextforge.test/v1/telemetry/routing-records",
        "bearer_token_file": str(token),
        "timeout_seconds": 0.2,
    }
    return raw


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")


def ok_upstream(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


@pytest.mark.asyncio
async def test_validate_reports_version_and_rejects_bad_configs(env, tmp_path):
    good = write_config(tmp_path, "good.yaml", raw_config("cerberus-2026-07-16.2"))
    bad_raw = raw_config("cerberus-2026-07-16.3")
    bad_raw["aliases"]["cerberus/free"]["candidates"][0]["model"] = "ghost"
    bad = write_config(tmp_path, "bad.yaml", bad_raw)
    doc = load_config_document(write_config(tmp_path, "active.yaml", raw_config("cerberus-2026-07-16.1")))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ok = await client.post("/admin/validate", json={"path": good})
            broken = await client.post("/admin/validate", json={"path": bad})

    assert ok.status_code == 200
    assert ok.json()["valid"] is True and ok.json()["version"] == "cerberus-2026-07-16.2"
    assert broken.status_code == 422
    assert broken.json()["valid"] is False and "ghost" in broken.json()["error"]


@pytest.mark.asyncio
async def test_activate_swaps_atomically_and_rollback_restores(env, tmp_path):
    v1 = write_config(tmp_path, "v1.yaml", raw_config("cerberus-2026-07-16.1", model="alpha-one"))
    v2 = write_config(tmp_path, "v2.yaml", raw_config("cerberus-2026-07-16.2", model="alpha-two"))
    served: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        served.append(httpx.Response(200, content=request.content).json()["model"])
        return httpx.Response(200, json={"choices": []})

    app = create_app(load_config_document(v1), http_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"model": "cerberus/free", "messages": []}
            await client.post("/v1/chat/completions", json=body)
            activated = await client.post("/admin/activate", json={"path": v2})
            await client.post("/v1/chat/completions", json=body)
            health_after_activate = await client.get("/health")
            rolled = await client.post("/admin/rollback")
            await client.post("/v1/chat/completions", json=body)
            health_after_rollback = await client.get("/health")

    assert activated.json()["active_version"] == "cerberus-2026-07-16.2"
    assert rolled.json()["active_version"] == "cerberus-2026-07-16.1"
    assert served == ["alpha-one", "alpha-two", "alpha-one"]
    assert health_after_activate.json()["config_version"] == "cerberus-2026-07-16.2"
    assert health_after_rollback.json()["config_version"] == "cerberus-2026-07-16.1"


@pytest.mark.asyncio
async def test_rollback_without_history_is_conflict(env, tmp_path):
    doc = load_config_document(write_config(tmp_path, "v1.yaml", raw_config("cerberus-2026-07-16.1")))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/admin/rollback")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_config_version_appears_in_telemetry_events(env, tmp_path):
    v1 = write_config(tmp_path, "v1.yaml", with_telemetry(raw_config("cerberus-2026-07-16.1"), tmp_path))
    events: list[dict] = []

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201)

    app = create_app(
        load_config_document(v1),
        http_transport=httpx.MockTransport(ok_upstream),
        telemetry_transport=httpx.MockTransport(telemetry),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})

    assert len(events) == 1
    assert events[0]["config_version"] == "cerberus-2026-07-16.1"


@pytest.mark.asyncio
async def test_shadow_decisions_recorded_never_served_never_called(env, tmp_path):
    """SPEC acceptance test 9: the shadow config's provider must see zero traffic."""
    active = write_config(
        tmp_path, "active.yaml", with_telemetry(raw_config("cerberus-2026-07-16.1", host="alpha.example"), tmp_path)
    )
    shadow = write_config(
        tmp_path, "shadow.yaml", raw_config("cerberus-2026-07-16.9", model="shadow-model", host="shadow.example")
    )
    hosts: list[str] = []
    events: list[dict] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json={"choices": []})

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201)

    app = create_app(
        load_config_document(active),
        http_transport=httpx.MockTransport(upstream),
        telemetry_transport=httpx.MockTransport(telemetry),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            armed = await client.post("/admin/shadow", json={"path": shadow})
            response = await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})
            cleared = await client.post("/admin/shadow", json={"path": None})

    assert armed.json()["shadow_version"] == "cerberus-2026-07-16.9"
    assert cleared.json()["shadow_version"] is None
    assert response.status_code == 200
    assert response.json()["cerberus"]["model"] == "alpha-one"  # served from active, never shadow
    assert hosts == ["alpha.example"]  # zero calls to shadow.example
    shadow_events = [e for e in events if e["outcome"] == "shadow"]
    assert len(shadow_events) == 1
    assert shadow_events[0]["config_version"] == "cerberus-2026-07-16.9"
    assert shadow_events[0]["model"] == "shadow-model"


@pytest.mark.asyncio
async def test_admin_status_reports_active_and_shadow(env, tmp_path):
    active = write_config(tmp_path, "v1.yaml", raw_config("cerberus-2026-07-16.1"))
    app = create_app(load_config_document(active), http_transport=httpx.MockTransport(ok_upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status = await client.get("/admin/status")
            config_dump = await client.get("/admin/config/active")

    payload = status.json()
    assert payload["active"]["version"] == "cerberus-2026-07-16.1"
    assert payload["active"]["checksum"].startswith("sha256:")
    assert payload["shadow"] is None
    assert "alpha" in config_dump.json()["providers"]
    assert "alpha-secret" not in config_dump.text  # env names only, never values


@pytest.mark.asyncio
async def test_shadow_records_a_miss_when_candidate_config_drops_the_alias(env, tmp_path):
    active = write_config(
        tmp_path, "active.yaml", with_telemetry(raw_config("cerberus-2026-07-16.1"), tmp_path)
    )
    shadow_raw = raw_config("cerberus-2026-07-16.9")
    shadow_raw["aliases"] = {
        "cerberus/other": {
            "mode": "free",
            "candidates": [{"provider": "alpha", "credential": "main", "model": "alpha-one"}],
        }
    }
    shadow = write_config(tmp_path, "shadow.yaml", shadow_raw)
    events: list[dict] = []

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201)

    app = create_app(
        load_config_document(active),
        http_transport=httpx.MockTransport(ok_upstream),
        telemetry_transport=httpx.MockTransport(telemetry),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/admin/shadow", json={"path": shadow})
            await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})

    misses = [e for e in events if e["outcome"] == "shadow" and e["pool"] == "missing"]
    assert len(misses) == 1 and misses[0]["model"] is None
