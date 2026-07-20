"""Authentik OIDC login for the admin console — Authorization Code + PKCE + sessions.

Authentik is the sole authority: Cerberus starts the flow, exchanges the code,
validates the id_token against Authentik's JWKS (issuer + audience + nonce + exp),
and gates on an admin group claim. The browser session is a Cerberus signed,
HttpOnly/Secure/SameSite cookie holding only an opaque server-side session id —
never a bearer token. Break-glass is an Authentik-issued admin-scoped bearer
presented in the Authorization header, validated the same way; Cerberus never
mints its own admin bearer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt

from cerberus.identity.session_store import (
    AdminSession,
    InMemorySessionStore,
    SessionStore,
)
from cerberus.registry.schema import AdminSSOConfig

__all__ = ["AdminSSO", "AdminSession"]

_PENDING_TTL = 600.0  # seconds an unfinished login may stay open


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class AdminSSO:
    def __init__(
        self,
        config: AdminSSOConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        store: SessionStore | None = None,
    ) -> None:
        self._c = config
        verify: str | bool = True
        if transport is None and config.ca_bundle:
            verify = config.ca_bundle
        self._http = httpx.AsyncClient(transport=transport, timeout=8.0, verify=verify)
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched = 0.0
        # pending logins + sessions live in a shared store so they survive across
        # workers and restarts; the in-memory default is single-worker only
        self._store: SessionStore = store if store is not None else InMemorySessionStore()

    def _secret(self) -> bytes:
        return os.environ.get(self._c.session_secret_env, "").encode()

    def _client_id(self) -> str:
        return os.environ.get(self._c.client_id_env, "")

    # -- OIDC start -----------------------------------------------------------

    def start_login(self) -> str:
        """Return the Authentik authorize URL; stash state/nonce/PKCE server-side."""

        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        now = time.time()
        self._store.put_pending(state, nonce, verifier, now + _PENDING_TTL)
        params = {
            "response_type": "code",
            "client_id": self._client_id(),
            "redirect_uri": str(self._c.redirect_uri),
            "scope": " ".join(self._c.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{str(self._c.authorize_url)}?{urlencode(params)}"

    # -- OIDC callback --------------------------------------------------------

    async def complete_login(self, code: str, state: str) -> AdminSession | None:
        """Exchange the code, validate the id_token, gate on admin group. None on any failure."""

        pending = self._store.pop_pending(state)  # single-use, atomic in the store
        if pending is None:
            return None
        nonce, verifier, expires = pending
        if time.time() > expires or not code:
            return None
        try:
            resp = await self._http.post(
                str(self._c.token_url),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": str(self._c.redirect_uri),
                    "client_id": self._client_id(),
                    "client_secret": os.environ.get(self._c.client_secret_env, ""),
                    "code_verifier": verifier,
                },
            )
            resp.raise_for_status()
            tokens = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            return None
        claims = await self._validate(id_token, expected_nonce=nonce)
        if claims is None or not self._is_admin(claims.get("groups")):
            return None  # unauthenticated, or authenticated-but-not-admin
        return AdminSession(
            sub=str(claims.get("sub", "")),
            email=str(claims.get("email", "")),
            name=str(claims.get("name") or claims.get("preferred_username") or ""),
            groups=list(claims.get("groups") or []),
            expires=time.time() + self._c.session_ttl_seconds,
        )

    # -- break-glass (Authentik admin bearer in the header) -------------------

    async def verify_break_glass(self, bearer: str) -> bool:
        """True for a valid Authentik token that is admin-group, admin-scoped, and short-lived.

        Beyond the signature/issuer/audience checks every token gets, break-glass
        adds two restrictions that a login id_token cannot satisfy: it must carry
        the dedicated break-glass scope, and its issued lifetime must be under the
        configured ceiling. Together these stop a captured id_token from acting as
        an admin bearer and stop a long-lived Authentik token from becoming a
        permanent admin key.
        """

        claims = await self._validate(bearer, expected_nonce=None)
        if claims is None or not self._is_admin(claims.get("groups")):
            return False
        return self._has_scope(claims) and self._within_lifetime(claims)

    def _has_scope(self, claims: dict) -> bool:
        """Accept either OAuth spelling: space-delimited `scope` or list-valued `scp`."""

        raw = claims.get("scope")
        granted = raw.split() if isinstance(raw, str) else []
        scp = claims.get("scp")
        if isinstance(scp, list):
            granted += [s for s in scp if isinstance(s, str)]
        elif isinstance(scp, str):
            granted += scp.split()
        return self._c.break_glass_scope in granted

    def _within_lifetime(self, claims: dict) -> bool:
        """Lifetime is judged at issue (exp - iat), not from now, so a nearly-expired
        long-lived token is still rejected rather than sneaking under the ceiling."""

        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, (int, float)) or not isinstance(iat, (int, float)):
            return False  # no iat => lifetime unprovable => refuse
        return 0 < exp - iat <= self._c.break_glass_max_lifetime_seconds

    # -- id_token / bearer validation (JWKS, stale-if-error) ------------------

    def _is_admin(self, groups: object) -> bool:
        return isinstance(groups, list) and any(g in self._c.admin_groups for g in groups)

    async def _refresh_keys(self) -> None:
        resp = await self._http.get(str(self._c.jwks_url))
        resp.raise_for_status()
        keys: dict[str, jwt.PyJWK] = {}
        for entry in resp.json().get("keys", []):
            try:
                key = jwt.PyJWK(entry)
            except jwt.exceptions.PyJWKError:
                continue
            kid = entry.get("kid")
            if kid and key.algorithm_name in self._c.algorithms:
                keys[kid] = key
        self._keys = keys
        self._fetched = time.time()

    async def _key_for(self, kid: str) -> jwt.PyJWK | None:
        if kid not in self._keys or time.time() - self._fetched > 300:
            try:
                await self._refresh_keys()
            except (httpx.HTTPError, ValueError):
                pass
        return self._keys.get(kid)

    async def _validate(self, token: str, *, expected_nonce: str | None) -> dict | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.exceptions.InvalidTokenError:
            return None
        if header.get("alg") not in self._c.algorithms:
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
                algorithms=list(self._c.algorithms),
                audience=self._client_id(),
                issuer=self._c.issuer,
                options={"require": ["exp", "aud", "iss"]},
            )
        # TypeError: PyJWT assumes well-typed claims, so a null/non-numeric `iat`
        # escapes InvalidTokenError — an unauthenticated caller must not be able to
        # turn a malformed token into a 500 on the admin gate.
        except (jwt.exceptions.InvalidTokenError, TypeError):
            return None
        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            return None
        return claims

    # -- sessions + signed cookie --------------------------------------------

    def create_session(self, session: AdminSession) -> str:
        sid = secrets.token_urlsafe(32)
        self._store.put_session(sid, session)
        return sid

    def cookie_value(self, sid: str) -> str:
        sig = hmac.new(self._secret(), sid.encode(), hashlib.sha256).hexdigest()
        return f"{sid}.{sig}"

    def session_from_cookie(self, cookie: str | None) -> AdminSession | None:
        if not cookie or "." not in cookie:
            return None
        sid, _, sig = cookie.rpartition(".")
        expected = hmac.new(self._secret(), sid.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        # the store returns None (and evicts) once expired
        return self._store.get_session(sid)

    def logout(self, cookie: str | None) -> None:
        if cookie and "." in cookie:
            self._store.delete_session(cookie.rpartition(".")[0])

    async def aclose(self) -> None:
        self._store.close()
        await self._http.aclose()
