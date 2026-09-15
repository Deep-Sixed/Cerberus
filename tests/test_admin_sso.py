"""Admin SSO — Authentik OIDC login, session cookie, admin-group gate, break-glass.

Authentik is mocked at the HTTP boundary (sso_transport serves JWKS + token). We
generate a keypair, sign id_tokens as Authentik would, and drive the full flow.
"""

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cerberus.app import create_app
from cerberus.registry import load_config_document
from tests.test_control import ok_upstream, raw_config, write_config  # noqa: E402

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
_JWK.update({"kid": "test-kid", "alg": "RS256", "use": "sig"})

ISSUER = "https://authentik.test/application/o/cerberus/"


def sign(claims: dict) -> str:
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "test-kid"})


def id_token(*, nonce, groups, aud="cerberus", iss=ISSUER, exp_delta=3600, sub="u1"):
    return sign({
        "iss": iss, "aud": aud, "sub": sub, "email": "op@example.com", "name": "Op",
        "groups": groups, "nonce": nonce, "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    })


def break_glass(*, groups=("authentik Admins",), scope="cerberus:admin", lifetime=900, drop=(), **kw):
    """An Authentik-issued break-glass bearer: admin group + dedicated scope + short life."""

    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": "cerberus", "sub": "ops", "groups": list(groups),
        "iat": now, "exp": now + lifetime,
    }
    if scope is not None:
        claims["scope"] = scope
    return sign({k: v for k, v in (claims | kw).items() if k not in drop})


def sso_block():
    return {
        "authorize_url": "https://authentik.test/application/o/authorize/",
        "token_url": "https://authentik.test/application/o/token/",
        "jwks_url": "https://authentik.test/application/o/cerberus/jwks/",
        "issuer": ISSUER,
        "client_id_env": "CB_OIDC_ID",
        "client_secret_env": "CB_OIDC_SECRET",
        "redirect_uri": "http://127.0.0.1:4000/admin/callback",
        "admin_groups": ["authentik Admins"],
        "session_secret_env": "CB_SESSION_SECRET",
    }


def make_app(monkeypatch, tmp_path, *, token_response):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CB_OIDC_ID", "cerberus")
    monkeypatch.setenv("CB_OIDC_SECRET", "shh")
    monkeypatch.setenv("CB_SESSION_SECRET", "session-signing-secret")
    raw = raw_config("cerberus-2026-07-16.1")
    raw["server"] = {"host": "127.0.0.1", "port": 4000}
    raw["admin_sso"] = sso_block()
    doc = load_config_document(write_config(tmp_path, "v.yaml", raw))

    def sso_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jwks/"):
            return httpx.Response(200, json={"keys": [_JWK]})
        if request.url.path.endswith("/token/"):
            return httpx.Response(200, json=token_response())
        return httpx.Response(404)

    return create_app(
        doc, http_transport=httpx.MockTransport(ok_upstream),
        sso_transport=httpx.MockTransport(sso_handler),
    )


REMOTE = ("203.0.113.9", 40001)


def remote_client(app):
    # https origin so httpx honors the Secure session cookie (browsers treat
    # 127.0.0.1 as a secure context and allow it over http too)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=REMOTE), base_url="https://127.0.0.1:4000")




@pytest.mark.asyncio
async def test_full_login_sets_session_and_grants_admin(monkeypatch, tmp_path):
    holder = {}
    app = make_app(monkeypatch, tmp_path, token_response=lambda: {"id_token": holder["tok"]})
    async with app.router.lifespan_context(app):
        async with remote_client(app) as client:
            # remote is denied before login
            assert (await client.get("/admin/status")).status_code == 401
            start = await client.get("/admin/login/start")
            q = parse_qs(urlparse(start.headers["location"]).query)
            assert start.headers["location"].startswith("https://authentik.test/application/o/authorize/")
            assert q["code_challenge_method"] == ["S256"] and "code_challenge" in q
            holder["tok"] = id_token(nonce=q["nonce"][0], groups=["authentik Admins"])
            cb = await client.get(f"/admin/callback?code=abc&state={q['state'][0]}")
            assert cb.status_code == 302 and cb.headers["location"] == "/admin/ui"
            cookie = cb.headers["set-cookie"]
            assert "httponly" in cookie.lower() and "secure" in cookie.lower() and "samesite=lax" in cookie.lower()
            # the session now grants the remote admin surface
            assert (await client.get("/admin/status")).status_code == 200
            assert (await client.get("/admin/providers")).status_code == 200


@pytest.mark.asyncio
async def test_authenticated_but_not_admin_is_rejected(monkeypatch, tmp_path):
    holder = {}
    app = make_app(monkeypatch, tmp_path, token_response=lambda: {"id_token": holder["tok"]})
    async with app.router.lifespan_context(app):
        async with remote_client(app) as client:
            start = await client.get("/admin/login/start")
            q = parse_qs(urlparse(start.headers["location"]).query)
            holder["tok"] = id_token(nonce=q["nonce"][0], groups=["some-other-group"])
            cb = await client.get(f"/admin/callback?code=abc&state={q['state'][0]}")
            assert cb.status_code == 403
            assert "set-cookie" not in cb.headers


@pytest.mark.asyncio
async def test_break_glass_admin_bearer_grants_without_session(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, token_response=lambda: {})
    async with app.router.lifespan_context(app):
        async with remote_client(app) as client:
            async def status(token):
                r = await client.get("/admin/status", headers={"authorization": f"Bearer {token}"})
                return r.status_code

            assert await status(break_glass()) == 200
            # a non-admin bearer is refused
            assert await status(break_glass(groups=["nope"])) == 401


@pytest.mark.asyncio
async def test_break_glass_requires_dedicated_scope_and_bounded_lifetime(monkeypatch, tmp_path):
    """Break-glass is narrower than a login: without these two checks any admin-group
    Authentik token — including a captured id_token or a long-lived service token —
    would be a standing admin key."""

    app = make_app(monkeypatch, tmp_path, token_response=lambda: {})
    async with app.router.lifespan_context(app):
        async with remote_client(app) as client:
            async def status(token):
                r = await client.get("/admin/status", headers={"authorization": f"Bearer {token}"})
                return r.status_code

            # a login id_token carries no scope claim, so it cannot be replayed here
            assert await status(id_token(nonce=None, groups=["authentik Admins"])) == 401
            assert await status(break_glass(scope=None)) == 401
            assert await status(break_glass(scope="openid profile")) == 401
            # correct scope alongside others is fine; `scp` is the other OAuth spelling
            assert await status(break_glass(scope="openid cerberus:admin")) == 200
            assert await status(break_glass(scope=None, scp=["cerberus:admin"])) == 200
            # issued lifetime above the ceiling is refused even while unexpired
            assert await status(break_glass(lifetime=86_400)) == 401
            assert await status(break_glass(lifetime=3600)) == 200
            # no iat => lifetime unprovable => refused (not a 500)
            assert await status(break_glass(drop=["iat"])) == 401
            # a malformed iat must also 401 cleanly rather than crash the gate
            assert await status(break_glass(iat="not-a-time")) == 401


@pytest.mark.asyncio
async def test_forged_and_wrong_audience_tokens_are_rejected(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, token_response=lambda: {})
    async with app.router.lifespan_context(app):
        async with remote_client(app) as client:
            wrong_aud = id_token(nonce=None, groups=["authentik Admins"], aud="someone-else")
            assert (await client.get("/admin/status", headers={"authorization": f"Bearer {wrong_aud}"})).status_code == 401
            wrong_iss = id_token(nonce=None, groups=["authentik Admins"], iss="https://evil/")
            assert (await client.get("/admin/status", headers={"authorization": f"Bearer {wrong_iss}"})).status_code == 401
            expired = id_token(nonce=None, groups=["authentik Admins"], exp_delta=-10)
            assert (await client.get("/admin/status", headers={"authorization": f"Bearer {expired}"})).status_code == 401


@pytest.mark.asyncio
async def test_callback_rejects_unknown_state(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, token_response=lambda: {"id_token": "x"})
    async with app.router.lifespan_context(app):
        async with remote_client(app) as client:
            assert (await client.get("/admin/callback?code=abc&state=never-issued")).status_code == 403


@pytest.mark.asyncio
async def test_root_redirects_to_login_and_login_page_has_no_tokens(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, token_response=lambda: {})
    async with app.router.lifespan_context(app):
        async with remote_client(app) as client:
            root = await client.get("/", follow_redirects=False)
            assert root.status_code in (302, 307) and root.headers["location"] == "/admin/login"
            page = await client.get("/admin/login")
            assert page.status_code == 200 and "Authentik" in page.text
            # no secret material embedded in the login page
            assert "session-signing-secret" not in page.text and "shh" not in page.text
            assert "cerberus_admin_session" not in page.text  # cookie name not leaked as a value


@pytest.mark.asyncio
async def test_docs_and_openapi_follow_the_admin_boundary(monkeypatch, tmp_path):
    """The schema enumerates every admin route and its shape, so it is admin
    information. FastAPI serves /docs and /openapi.json publicly by default;
    Cerberus must not."""

    app = make_app(monkeypatch, tmp_path, token_response=lambda: {})
    async with app.router.lifespan_context(app):
        async with remote_client(app) as client:
            for path in ("/docs", "/openapi.json"):
                assert (await client.get(path)).status_code == 401, path
                authorized = await client.get(
                    path, headers={"authorization": f"Bearer {break_glass()}"}
                )
                assert authorized.status_code == 200, path
            # and the schema is real, not a stub
            schema = await client.get("/openapi.json", headers={"authorization": f"Bearer {break_glass()}"})
            assert "/v1/chat/completions" in schema.json()["paths"]
