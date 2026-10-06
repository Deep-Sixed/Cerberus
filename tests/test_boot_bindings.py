"""Infrastructure bindings follow the boot file, never a revision restored from SQLite.

cli.py binds the listen host and port from the file the process was started
with. If auth settings came from the restored active revision instead, an
operator who edited the file to a public host plus an api token and restarted
would get a public listener enforcing the old revision's (absent) token.
"""

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import load_config_document
from tests.test_control import ok_upstream, raw_config, write_config


def _config(version, state_path, server):
    raw = raw_config(version)
    raw["state"] = {"path": state_path}
    raw.pop("identities", None)
    raw["server"] = server
    return raw


def _client(app, host):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 40001)), base_url="http://test")


@pytest.mark.asyncio
async def test_restart_enforces_the_boot_files_api_token(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("API_TOKEN", "api-secret")
    state_path = str(tmp_path / "state.sqlite3")

    first = _config("cerberus-2026-07-16.1", state_path, {"host": "127.0.0.1"})
    app = create_app(
        load_config_document(write_config(tmp_path, "boot.yaml", first)),
        http_transport=httpx.MockTransport(ok_upstream),
    )
    async with app.router.lifespan_context(app):
        pass

    # the operator hardens the boot file and restarts
    hardened = _config(
        "cerberus-2026-07-16.2", state_path, {"host": "0.0.0.0", "api_token_env": "API_TOKEN"}
    )
    restarted = create_app(
        load_config_document(write_config(tmp_path, "boot.yaml", hardened)),
        http_transport=httpx.MockTransport(ok_upstream),
    )
    alias = next(iter(hardened["aliases"]))
    body = {"model": alias, "messages": [{"role": "user", "content": "hi"}]}
    async with restarted.router.lifespan_context(restarted):
        # routing policy still follows the restored revision
        assert restarted.state.lifecycle.active.version == "cerberus-2026-07-16.1"
        async with _client(restarted, "203.0.113.9") as client:
            denied = await client.post("/v1/chat/completions", json=body)
            assert denied.status_code == 401
            allowed = await client.post(
                "/v1/chat/completions", json=body, headers={"authorization": "Bearer api-secret"}
            )
            assert allowed.status_code == 200, allowed.text
