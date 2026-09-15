"""Session 5 acceptance tests — Authentik JWT verification (local JWKS, fail closed).

Authentik owns authentication; Cerberus validates locally with cached signing
keys and is never on the per-request path to Authentik.
"""

import json
import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cerberus.app import create_app
from cerberus.registry import CerberusConfig

ISSUER = "https://authentik.test/application/o/cerberus/"
AUDIENCE = "cerberus"


class Keypair:
    def __init__(self, kid: str):
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict:
        return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key())), "kid": self.kid, "use": "sig", "alg": "RS256"}

    def token(self, *, client_id="robot-recon", aud=AUDIENCE, iss=ISSUER, exp_delta=300, alg="RS256") -> str:
        claims = {
            "iss": iss,
            "aud": aud,
            "exp": int(time.time()) + exp_delta,
            "iat": int(time.time()) - 5,
            "sub": str(uuid.uuid4()),
            "client_id": client_id,
        }
        return jwt.encode(claims, self.private, algorithm=alg, headers={"kid": self.kid})


def jwt_config(monkeypatch: pytest.MonkeyPatch) -> CerberusConfig:
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("CB_KEY_STATIC", "cb-static-key")
    return CerberusConfig.model_validate(
        {
            "metadata": {"version": "cerberus-2026-07-16.1"},
            "authentik": {"issuer": ISSUER, "jwks_url": "https://authentik.test/jwks", "audience": AUDIENCE},
            "providers": {
                "alpha": {
                    "base_url": "https://alpha.example/v1",
                    "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                    "models": {"alpha-free": {"cost_tier": "free"}},
                },
            },
            "aliases": {
                "cerberus/free": {
                    "mode": "free",
                    "candidates": [{"provider": "alpha", "credential": "main", "model": "alpha-free"}],
                },
            },
            "identities": {
                "recon": {
                    "jwt_client_id": "robot-recon",
                    "allowed_modes": ["free"],
                    "allowed_aliases": ["cerberus/free"],
                },
                "legacy": {
                    "credential_env": "CB_KEY_STATIC",
                    "allowed_modes": ["free"],
                    "allowed_aliases": ["cerberus/free"],
                },
            },
        }
    )


def jwks_transport(keys: list[Keypair], failures: dict | None = None) -> httpx.MockTransport:
    state = failures if failures is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        state["fetches"] = state.get("fetches", 0) + 1
        if state.get("down"):
            raise httpx.ConnectError("jwks endpoint down", request=request)
        return httpx.Response(200, json={"keys": [key.jwk() for key in keys]})

    return httpx.MockTransport(handler)


def ok_upstream(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


async def post_token(app, token: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/v1/chat/completions",
                json={"model": "cerberus/free", "messages": []},
                headers={"authorization": f"Bearer {token}"},
            )


@pytest.mark.asyncio
async def test_valid_jwt_resolves_identity_by_client_id(monkeypatch):
    key = Keypair("k1")
    app = create_app(
        jwt_config(monkeypatch),
        http_transport=httpx.MockTransport(ok_upstream),
        jwks_transport=jwks_transport([key]),
    )
    response = await post_token(app, key.token())
    assert response.status_code == 200
    assert response.json()["cerberus"]["identity"] == "recon"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"exp_delta": -60},          # expired
        {"aud": "contextforge"},     # wrong audience (replay from another service)
        {"iss": "https://evil.test/"},  # wrong issuer
        {"client_id": "robot-unknown"},  # no identity match
    ],
)
async def test_invalid_tokens_are_rejected(monkeypatch, kwargs):
    key = Keypair("k1")
    app = create_app(
        jwt_config(monkeypatch),
        http_transport=httpx.MockTransport(ok_upstream),
        jwks_transport=jwks_transport([key]),
    )
    response = await post_token(app, key.token(**kwargs))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_symmetric_algorithms_are_never_accepted(monkeypatch):
    """HS256 tokens (the client-federation population) must not blur into robot auth."""
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300, "client_id": "robot-recon"},
        "guessable-shared-secret-32-bytes-long!",
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    key = Keypair("k1")
    app = create_app(
        jwt_config(monkeypatch),
        http_transport=httpx.MockTransport(ok_upstream),
        jwks_transport=jwks_transport([key]),
    )
    response = await post_token(app, forged)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_key_rotation_overlap_via_kid(monkeypatch):
    old, new = Keypair("old"), Keypair("new")
    app = create_app(
        jwt_config(monkeypatch),
        http_transport=httpx.MockTransport(ok_upstream),
        jwks_transport=jwks_transport([old, new]),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for key in (old, new):
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "cerberus/free", "messages": []},
                    headers={"authorization": f"Bearer {key.token()}"},
                )
                assert response.status_code == 200


@pytest.mark.asyncio
async def test_authentik_outage_after_cache_still_validates(monkeypatch):
    key = Keypair("k1")
    state: dict = {}
    app = create_app(
        jwt_config(monkeypatch),
        http_transport=httpx.MockTransport(ok_upstream),
        jwks_transport=jwks_transport([key], failures=state),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/v1/chat/completions",
                json={"model": "cerberus/free", "messages": []},
                headers={"authorization": f"Bearer {key.token()}"},
            )
            state["down"] = True  # Authentik goes away; cached keys must keep working
            second = await client.post(
                "/v1/chat/completions",
                json={"model": "cerberus/free", "messages": []},
                headers={"authorization": f"Bearer {key.token()}"},
            )
    assert first.status_code == 200
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_unknown_kid_with_unreachable_jwks_fails_closed(monkeypatch):
    key, stranger = Keypair("k1"), Keypair("k2")
    state: dict = {"down": True}
    app = create_app(
        jwt_config(monkeypatch),
        http_transport=httpx.MockTransport(ok_upstream),
        jwks_transport=jwks_transport([key], failures=state),
    )
    response = await post_token(app, stranger.token())
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_static_and_jwt_identities_coexist(monkeypatch):
    key = Keypair("k1")
    app = create_app(
        jwt_config(monkeypatch),
        http_transport=httpx.MockTransport(ok_upstream),
        jwks_transport=jwks_transport([key]),
    )
    response = await post_token(app, "cb-static-key")
    assert response.status_code == 200
    assert response.json()["cerberus"]["identity"] == "legacy"


def test_identity_requires_exactly_one_credential_source(monkeypatch):
    raw = {
        "metadata": {"version": "cerberus-2026-07-16.1"},
        "providers": {
            "alpha": {
                "base_url": "https://alpha.example/v1",
                "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                "models": {"alpha-free": {"cost_tier": "free"}},
            },
        },
        "aliases": {
            "cerberus/free": {
                "mode": "free",
                "candidates": [{"provider": "alpha", "credential": "main", "model": "alpha-free"}],
            },
        },
        "identities": {
            "broken": {"allowed_modes": ["free"], "allowed_aliases": ["cerberus/free"]},
        },
    }
    with pytest.raises(ValueError, match="credential_env or jwt_client_id"):
        CerberusConfig.model_validate(raw)
    raw["identities"]["broken"]["jwt_client_id"] = "robot-x"
    with pytest.raises(ValueError, match="authentik"):
        CerberusConfig.model_validate(raw)  # jwt identity without authentik block


@pytest.mark.asyncio
async def test_new_kid_published_after_cache_fill_is_usable_via_refresh():
    """Refresh on unknown kid (rate-limited): rotation works without waiting out the TTL."""
    from cerberus.identity import AuthentikVerifier
    from cerberus.registry.schema import AuthentikConfig

    old, new = Keypair("old"), Keypair("new")
    published = [old]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [key.jwk() for key in published]})

    config = AuthentikConfig(issuer=ISSUER, jwks_url="https://authentik.test/jwks", audience=AUDIENCE)
    verifier = AuthentikVerifier(config, httpx.MockTransport(handler))
    try:
        assert await verifier.verify(old.token()) == "robot-recon"
        published.append(new)  # rotation while the cache is still fresh
        assert await verifier.verify(new.token()) is None  # rate limit: no immediate re-fetch
        verifier._min_refresh_interval = 0.0
        assert await verifier.verify(new.token()) == "robot-recon"
    finally:
        await verifier.aclose()
