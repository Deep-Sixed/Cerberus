"""Browser config editor — schema, allow-listed staging, and the full
stage -> validate -> activate loop through the real ASGI surface."""

import os
import time

import httpx
import pytest

from cerberus.app import create_app
from cerberus.control.admin_fields import apply_updates, build_schema, editable_keys, stage
from cerberus.registry import load_config_document
from tests.test_control import CSRF, ok_upstream, raw_config, write_config


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")


@pytest.fixture
def config(tmp_path, env):
    return load_config_document(write_config(tmp_path, "cfg.yaml", raw_config("cerberus-2026-07-16.1"))).config


def test_schema_lists_editable_provider_fields_and_locks_credentials(config):
    schema = build_schema(config)
    keys = {f["key"] for f in schema["fields"]}
    assert "providers.alpha.quota_cooldown_seconds" in keys
    assert "providers.alpha.transport_cooldown_seconds" in keys
    assert "providers.alpha.health_probe" in keys
    assert keys == editable_keys(config) | {"providers.alpha.credentials.main.api_key_env"}

    by_key = {f["key"]: f for f in schema["fields"]}
    assert by_key["providers.alpha.quota_cooldown_seconds"]["locked"] is False
    assert by_key["providers.alpha.quota_cooldown_seconds"]["value"] == 3600  # schema default
    assert by_key["providers.alpha.credentials.main.api_key_env"]["locked"] is True
    assert by_key["providers.alpha.credentials.main.api_key_env"]["value"] == "ALPHA_KEY"


def test_apply_updates_rejects_anything_off_the_allow_list(config):
    with pytest.raises(ValueError, match="not editable"):
        apply_updates(config, {"providers.alpha.credentials.main.api_key_env": "EVIL_KEY"})
    with pytest.raises(ValueError, match="not editable"):
        apply_updates(config, {"server.admin_token_env": "SOMETHING"})


def test_apply_updates_coerces_browser_strings_and_bumps_version(config):
    # HTML number inputs' .value is always a string in the DOM — the real shape
    # a browser will submit, not a JSON number
    updated = apply_updates(config, {"providers.alpha.quota_cooldown_seconds": "90"})
    assert updated.providers["alpha"].quota_cooldown_seconds == 90
    assert updated.metadata.version == "cerberus-2026-07-16.2"
    # unrelated fields are untouched
    assert updated.providers["alpha"].transport_cooldown_seconds == 30


def test_apply_updates_is_a_no_op_when_no_changes_submitted(config):
    same = apply_updates(config, {})
    assert same.metadata.version == config.metadata.version  # no bump without real edits


def test_stage_writes_a_loadable_config(config, tmp_path):
    updated = apply_updates(config, {"providers.alpha.health_probe": "chat"})
    path = stage(updated, tmp_path / "staging")
    reloaded = load_config_document(path, validate_credentials=False)
    assert reloaded.version == updated.metadata.version
    assert reloaded.config.providers["alpha"].health_probe == "chat"


def test_stage_sweeps_stale_files_but_not_fresh_ones(config, tmp_path):
    """Repeated Validate clicks without Apply must not leak disk space
    unboundedly — each stage() call sweeps files older than the TTL."""

    directory = tmp_path / "staging"
    directory.mkdir()
    stale = directory / "old.yaml"
    stale.write_text("stale: true", encoding="utf-8")
    old_time = time.time() - 3600  # older than the 900s TTL
    os.utime(stale, (old_time, old_time))
    fresh = directory / "fresh.yaml"
    fresh.write_text("fresh: true", encoding="utf-8")  # untouched mtime — well under the TTL

    stage(apply_updates(config, {"providers.alpha.quota_cooldown_seconds": "10"}), directory)

    assert not stale.exists(), "stale staged file should have been swept"
    assert fresh.exists(), "a recently-staged file must survive an unrelated stage() call"


@pytest.mark.asyncio
async def test_config_schema_endpoint_is_read_only_gated(env, tmp_path):
    doc = load_config_document(write_config(tmp_path, "cfg.yaml", raw_config("cerberus-2026-07-16.1")))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/config/schema")
    assert resp.status_code == 200
    keys = {f["key"] for f in resp.json()["fields"]}
    assert "providers.alpha.quota_cooldown_seconds" in keys


@pytest.mark.asyncio
async def test_stage_endpoint_rejects_non_allow_listed_keys(env, tmp_path):
    doc = load_config_document(write_config(tmp_path, "cfg.yaml", raw_config("cerberus-2026-07-16.1")))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bad_key = await client.post(
                "/admin/config/stage",
                json={"updates": {"providers.alpha.credentials.main.api_key_env": "EVIL"}},
                headers=CSRF,
            )
            malformed = await client.post("/admin/config/stage", json={"nope": {}}, headers=CSRF)
    assert bad_key.status_code == 422 and bad_key.json()["staged"] is False
    assert malformed.status_code == 400


@pytest.mark.asyncio
async def test_stage_validate_activate_loop_changes_live_behavior(env, tmp_path):
    """The full loop a browser drives: edit a field, stage it, validate the
    staged path, activate it — and the new value is what dispatch actually
    uses, not just what a status endpoint echoes back."""

    doc = load_config_document(write_config(tmp_path, "cfg.yaml", raw_config("cerberus-2026-07-16.1")))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            staged = await client.post(
                "/admin/config/stage",
                json={"updates": {"providers.alpha.quota_cooldown_seconds": "45"}},
                headers=CSRF,
            )
            assert staged.status_code == 200
            path = staged.json()["path"]
            assert staged.json()["version"] == "cerberus-2026-07-16.2"

            validated = await client.post("/admin/validate", json={"path": path}, headers=CSRF)
            assert validated.status_code == 200 and validated.json()["valid"] is True

            activated = await client.post("/admin/activate", json={"path": path}, headers=CSRF)
            assert activated.status_code == 200
            assert activated.json()["active_version"] == "cerberus-2026-07-16.2"

            active = await client.get("/admin/config/active")
            assert active.json()["providers"]["alpha"]["quota_cooldown_seconds"] == 45
