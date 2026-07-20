"""R9 + R10: SSO across workers and across restarts, end to end through the ASGI
surface (real cookie signing, real admin_gate), not just the store in isolation.

Two create_app instances over one shared state DB stand in for two uvicorn
workers. The signing secret is env-based, so both honor each other's cookies.
"""

import httpx
import pytest
from urllib.parse import parse_qs, urlparse

from cerberus.app import create_app
from cerberus.registry import load_config_document
from tests.test_admin_sso import REMOTE, id_token, sso_block
from tests.test_control import ok_upstream, raw_config, write_config


def build_worker(config_path, token_holder):
    """A worker over a fixed on-disk config, with Authentik mocked at the HTTP edge."""

    def sso_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jwks/"):
            from tests.test_admin_sso import _JWK
            return httpx.Response(200, json={"keys": [_JWK]})
        if request.url.path.endswith("/token/"):
            return httpx.Response(200, json={"id_token": token_holder["tok"]})
        return httpx.Response(404)

    doc = load_config_document(config_path)
    return create_app(
        doc,
        http_transport=httpx.MockTransport(ok_upstream),
        sso_transport=httpx.MockTransport(sso_handler),
    )


def remote_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=REMOTE),
        base_url="https://127.0.0.1:4000",
    )


def sso_config(tmp_path):
    """One config shared by every worker: SSO on, sessions on a shared SQLite file."""

    raw = raw_config("cerberus-2026-07-19.1")
    raw["server"] = {"host": "127.0.0.1", "port": 4000}
    raw["admin_sso"] = sso_block()
    raw["state"] = {"path": str(tmp_path / "shared-state.sqlite3")}
    return write_config(tmp_path, "cfg.yaml", raw)


def set_sso_env(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CB_OIDC_ID", "cerberus")
    monkeypatch.setenv("CB_OIDC_SECRET", "shh")
    monkeypatch.setenv("CB_SESSION_SECRET", "session-signing-secret")


async def login(app, holder):
    """Drive a full Authorization-Code login against `app`; return the session cookie header."""

    async with remote_client(app) as client:
        start = await client.get("/admin/login/start")
        q = parse_qs(urlparse(start.headers["location"]).query)
        holder["tok"] = id_token(nonce=q["nonce"][0], groups=["authentik Admins"])
        cb = await client.get(f"/admin/callback?code=abc&state={q['state'][0]}")
        assert cb.status_code == 302, cb.status_code
        return cb.headers["set-cookie"].split(";")[0]  # name=value


@pytest.mark.asyncio
async def test_r9_session_and_pending_cross_workers(monkeypatch, tmp_path):
    """R9: a login started on worker A callbacks on worker B, and the resulting
    session — created on B — authorizes on A. Neither is possible with per-process state."""

    set_sso_env(monkeypatch)
    cfg = sso_config(tmp_path)
    holder = {}
    worker_a = build_worker(cfg, holder)
    worker_b = build_worker(cfg, holder)

    async with worker_a.router.lifespan_context(worker_a), worker_b.router.lifespan_context(worker_b):
        # start on A ...
        async with remote_client(worker_a) as ca:
            start = await ca.get("/admin/login/start")
            q = parse_qs(urlparse(start.headers["location"]).query)
        holder["tok"] = id_token(nonce=q["nonce"][0], groups=["authentik Admins"])
        # ... callback on B (pending state must be visible cross-worker)
        async with remote_client(worker_b) as cb:
            done = await cb.get(f"/admin/callback?code=abc&state={q['state'][0]}")
            assert done.status_code == 302, "callback on B could not see pending from A"
            cookie = done.headers["set-cookie"].split(";")[0]
        # session created on B authorizes on A
        async with remote_client(worker_a) as ca:
            headers = {"cookie": cookie}
            assert (await ca.get("/admin/status", headers=headers)).status_code == 200
        # single-use is global: replaying the same state on A is refused (403 = the
        # callback's login-failed response, since the pending row is already consumed)
        async with remote_client(worker_a) as ca:
            replay = await ca.get(f"/admin/callback?code=abc&state={q['state'][0]}")
            assert replay.status_code == 403, "pending state was replayable across workers"
            assert "set-cookie" not in replay.headers


@pytest.mark.asyncio
async def test_r10_session_survives_worker_restart(monkeypatch, tmp_path):
    """R10: a session minted before a container recreation is still honored by a
    freshly-built worker over the same state file, and logout is likewise durable."""

    set_sso_env(monkeypatch)
    cfg = sso_config(tmp_path)
    holder = {}

    old = build_worker(cfg, holder)
    async with old.router.lifespan_context(old):
        cookie = await login(old, holder)
        async with remote_client(old) as c:
            assert (await c.get("/admin/status", headers={"cookie": cookie})).status_code == 200
    # `old` is now torn down — simulate the container being recreated

    fresh = build_worker(cfg, holder)
    async with fresh.router.lifespan_context(fresh):
        async with remote_client(fresh) as c:
            assert (await c.get("/admin/status", headers={"cookie": cookie})).status_code == 200, \
                "session did not survive restart"
            # logout on the fresh worker invalidates the persisted session
            await c.get("/admin/logout", headers={"cookie": cookie})
            assert (await c.get("/admin/status", headers={"cookie": cookie})).status_code == 401
