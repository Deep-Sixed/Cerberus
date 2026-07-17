"""Authentik JWT verification — local validation with cached JWKS.

Authentik is the sole issuer; Cerberus never mints or self-signs tokens and is
never on the per-request path to Authentik. Keys are cached with a TTL,
refreshed asynchronously on expiry or unknown kid, and kept (stale) through an
Authentik outage — expiry/audience/issuer checks still apply. Only asymmetric
algorithms are accepted: the HS256 client-federation token population must
never blur into robot authentication.
"""

from __future__ import annotations

import time

import httpx
import jwt

from cerberus.registry.schema import AuthentikConfig


class AuthentikVerifier:
    def __init__(self, config: AuthentikConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._client = httpx.AsyncClient(transport=transport, timeout=5.0)
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float = 0.0
        self._refresh_attempted_at: float = 0.0
        self._min_refresh_interval: float = 10.0  # forged kids must not hammer Authentik

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _refresh(self) -> None:
        response = await self._client.get(str(self._config.jwks_url))
        response.raise_for_status()
        keys: dict[str, jwt.PyJWK] = {}
        for entry in response.json().get("keys", []):
            try:
                key = jwt.PyJWK(entry)
            except jwt.exceptions.PyJWKError:
                continue
            kid = entry.get("kid")
            if kid and key.algorithm_name in self._config.algorithms:
                keys[kid] = key
        self._keys = keys
        self._fetched_at = time.time()

    async def _key_for(self, kid: str) -> jwt.PyJWK | None:
        now = time.time()
        stale = now - self._fetched_at > self._config.cache_ttl_seconds
        # refresh when the cache aged out OR the kid is unknown (key rotation),
        # rate-limited so unknown kids cannot turn into an Authentik hammer
        wants_refresh = stale or kid not in self._keys
        if wants_refresh and now - self._refresh_attempted_at >= self._min_refresh_interval:
            self._refresh_attempted_at = now
            try:
                await self._refresh()
            except (httpx.HTTPError, ValueError):
                # outage: keep validating with cached keys (stale-if-error)
                pass
        return self._keys.get(kid)

    async def verify(self, token: str) -> str | None:
        """Return the token's client_id when valid; None on any failure (fail closed)."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.exceptions.InvalidTokenError:
            return None
        algorithm = header.get("alg")
        if algorithm not in self._config.algorithms:
            return None
        kid = header.get("kid")
        if not isinstance(kid, str):
            return None
        key = await self._key_for(kid)
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=list(self._config.algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
                options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.exceptions.InvalidTokenError:
            return None
        client_id = claims.get("client_id") or claims.get("azp")
        return client_id if isinstance(client_id, str) else None
