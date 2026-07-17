"""Authorization gate repairs — uvicorn proxy-header trust and admin credential validation."""

import asyncio

import httpx
import pytest
import uvicorn
from pydantic import ValidationError

import cerberus.cli
from cerberus.app import create_app
from cerberus.registry import CerberusConfig, load_config_document
from cerberus.registry.loader import require_configured_credentials, required_credential_envs
from tests.test_control import ok_upstream, raw_config, write_config


def tokened_config(tmp_path, name="v1.yaml", api_env="CERBERUS_API_TOKEN", admin_env="CERBERUS_ADMIN_TOKEN"):
    raw = raw_config("cerberus-2026-07-16.1")
    raw["server"] = {"host": "127.0.0.1", "port": 4000, "api_token_env": api_env, "admin_token_env": admin_env}
    return write_config(tmp_path, name, raw)


# -- loader/schema validation -------------------------------------------------


def test_admin_credential_is_required_by_the_loader(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CERBERUS_API_TOKEN", "inference-token")
    monkeypatch.delenv("CERBERUS_ADMIN_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="CERBERUS_ADMIN_TOKEN"):
        load_config_document(tokened_config(tmp_path))


def test_blank_admin_credential_fails_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CERBERUS_API_TOKEN", "inference-token")
    monkeypatch.setenv("CERBERUS_ADMIN_TOKEN", "   ")
    with pytest.raises(RuntimeError, match="CERBERUS_ADMIN_TOKEN"):
        load_config_document(tokened_config(tmp_path))


def test_admin_env_name_must_differ_from_api_env_name():
    with pytest.raises(ValidationError, match="different environment variable"):
        CerberusConfig.model_validate(
            {
                **raw_config("cerberus-2026-07-16.1"),
                "server": {
                    "host": "127.0.0.1",
                    "port": 4000,
                    "api_token_env": "CERBERUS_API_TOKEN",
                    "admin_token_env": "CERBERUS_API_TOKEN",
                },
            }
        )


def test_same_secret_value_under_two_names_fails_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CERBERUS_API_TOKEN", "identical-token-value")
    monkeypatch.setenv("CERBERUS_ADMIN_TOKEN", "identical-token-value")
    with pytest.raises(RuntimeError) as excinfo:
        load_config_document(tokened_config(tmp_path))
    message = str(excinfo.value)
    assert "distinct credentials" in message
    assert "identical-token-value" not in message  # no secret values in errors


def test_distinct_credentials_load_and_are_both_required(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CERBERUS_API_TOKEN", "inference-token")
    monkeypatch.setenv("CERBERUS_ADMIN_TOKEN", "admin-secret")
    document = load_config_document(tokened_config(tmp_path))
    assert "CERBERUS_ADMIN_TOKEN" in required_credential_envs(document.config)
    assert "CERBERUS_API_TOKEN" in required_credential_envs(document.config)
    require_configured_credentials(document.config)  # does not raise


# -- behavior with validated credentials --------------------------------------


def make_tokened_app(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CERBERUS_API_TOKEN", "inference-token")
    monkeypatch.setenv("CERBERUS_ADMIN_TOKEN", "admin-secret")
    doc = load_config_document(tokened_config(tmp_path))
    return create_app(doc, http_transport=httpx.MockTransport(ok_upstream))


REMOTE = ("203.0.113.9", 40001)
MUTATION_URLS = ("/admin/validate", "/admin/activate", "/admin/rollback", "/admin/shadow")


@pytest.mark.asyncio
async def test_validated_inference_token_still_denied_on_all_mutations(monkeypatch, tmp_path):
    app = make_tokened_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=REMOTE)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as remote:
            for url in MUTATION_URLS:
                response = await remote.post(url, json={}, headers={"authorization": "Bearer inference-token"})
                assert response.status_code == 403, url


@pytest.mark.asyncio
async def test_validated_admin_token_keeps_read_only_remote_and_loopback_mutation(monkeypatch, tmp_path):
    app = make_tokened_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        remote_transport = httpx.ASGITransport(app=app, client=REMOTE)
        async with httpx.AsyncClient(transport=remote_transport, base_url="http://test") as remote:
            admin = {"authorization": "Bearer admin-secret"}
            assert (await remote.get("/admin/events", headers=admin)).status_code == 200
            for url in MUTATION_URLS:
                assert (await remote.post(url, json={}, headers=admin)).status_code == 403, url
        local_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40001))
        async with httpx.AsyncClient(transport=local_transport, base_url="http://test") as local:
            assert (await local.post("/admin/validate", json={})).status_code == 400  # auth ok, body invalid


# -- uvicorn startup path -----------------------------------------------------


def test_cli_serve_disables_proxy_header_processing(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    config_path = write_config(tmp_path, "v1.yaml", raw_config("cerberus-2026-07-16.1"))
    monkeypatch.setattr("sys.argv", ["cerberus", "serve", "--config", config_path])
    cerberus.cli.main()
    assert captured["proxy_headers"] is False
    assert captured["host"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_real_bound_server_ignores_forwarding_headers(monkeypatch, tmp_path):
    """Discriminating probe: from a real loopback TCP connection, send headers a
    proxy-header-processing server would use to rewrite request.client to a
    remote address. If any were honored, the loopback mutation would be denied;
    with processing off, the true transport peer keeps authorization."""

    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    doc = load_config_document(write_config(tmp_path, "v1.yaml", raw_config("cerberus-2026-07-16.1")))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, proxy_headers=False, log_level="error")
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            spoofs = [
                {"x-forwarded-for": "203.0.113.9"},  # would demote loopback if processed
                {"x-forwarded-for": "127.0.0.1"},
                {"x-real-ip": "127.0.0.1"},
                {"forwarded": "for=127.0.0.1"},
            ]
            for headers in spoofs:
                response = await client.post("/admin/rollback", headers=headers)
                # authorization passed on the true loopback peer in every case;
                # 409 = empty rollback history, never 401/403
                assert response.status_code == 409, headers
            assert (await client.get("/health")).status_code == 200
    finally:
        server.should_exit = True
        await task
