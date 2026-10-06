"""Loopback trust requires a Host naming this machine (DNS-rebinding defence).

A hostile page open in a local operator's browser can rebind its own hostname
to 127.0.0.1. Its requests then arrive from a loopback peer, are same-origin,
and can set X-Cerberus-CSRF — so the peer address alone must not admit them.
"""

import httpx
import pytest

from cerberus.app import create_app, is_local_host_header
from tests.test_app import make_config

CSRF = {"x-cerberus-csrf": "1"}


@pytest.mark.parametrize(
    "header",
    ["localhost", "LOCALHOST:4111", "127.0.0.1", "127.0.0.1:4000", "127.8.9.10", "[::1]:4111", "::1", "app.localhost"],
)
def test_local_host_headers(header):
    assert is_local_host_header(header)


@pytest.mark.parametrize("header", [None, "", "evil.example", "evil.example:4111", "127.0.0.1.evil.example", "203.0.113.7"])
def test_foreign_host_headers(header):
    assert not is_local_host_header(header)


def test_trusted_names_are_opt_in():
    assert is_local_host_header("gateway.lan:4111", frozenset({"gateway.lan"}))


async def _get(app, path, host, method="GET", **kwargs):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40001))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as client:
            return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_rebound_hostname_gets_no_admin_access(monkeypatch):
    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert (await _get(app, "/admin/config/active", "evil.example:4000")).status_code == 403
    rollback = await _get(app, "/admin/rollback", "evil.example:4000", method="POST", headers=CSRF)
    assert rollback.status_code == 403
    assert (await _get(app, "/admin/config/active", "localhost:4000")).status_code == 200


@pytest.mark.asyncio
async def test_rebound_hostname_cannot_use_tokenless_inference(monkeypatch):
    app = create_app(
        make_config(monkeypatch),
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []})),
    )
    body = {"model": "cerberus/main", "messages": [{"role": "user", "content": "hi"}]}
    denied = await _get(app, "/v1/chat/completions", "evil.example:4000", method="POST", json=body)
    assert denied.status_code == 401
    allowed = await _get(app, "/v1/chat/completions", "127.0.0.1:4000", method="POST", json=body)
    assert allowed.status_code == 200, allowed.text
