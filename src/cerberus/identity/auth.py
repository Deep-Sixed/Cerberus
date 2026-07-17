"""Client credential resolution and deny-by-default authorization.

Cerberus owns authorization; issuance is external (static cb- keys via the
file-secrets bridge now, Authentik JWTs in S5). The wire form — Authorization:
Bearer or x-api-key — is transport metadata, not identity: both resolve to the
same credential record. Comparison is constant-time across ALL identities (no
short-circuit), so response timing does not reveal which credentials exist.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from fastapi import Request

from cerberus.registry.schema import Alias, CerberusConfig, Identity


@dataclass(frozen=True, slots=True)
class IdentityContext:
    name: str
    identity: Identity


def supplied_credential(request: Request) -> str | None:
    bearer = request.headers.get("authorization", "")
    if bearer.startswith("Bearer "):
        value = bearer.removeprefix("Bearer ").strip()
        if value:
            return value
    api_key = request.headers.get("x-api-key", "").strip()
    return api_key or None


def resolve_identity(config: CerberusConfig, request: Request) -> IdentityContext | None:
    supplied = supplied_credential(request)
    if not supplied:
        return None
    supplied_bytes = supplied.encode("utf-8", errors="replace")
    matched: IdentityContext | None = None
    for name, identity in config.identities.items():
        expected = os.environ.get(identity.credential_env, "").strip()
        if not expected:
            continue
        # compare every identity without short-circuiting
        if secrets.compare_digest(supplied_bytes, expected.encode("utf-8", errors="replace")):
            matched = IdentityContext(name=name, identity=identity)
    return matched


def authorization_error(context: IdentityContext, alias_name: str, alias: Alias) -> str | None:
    """None when authorized; otherwise the denial reason. Allow-lists only."""
    if alias_name not in context.identity.allowed_aliases:
        return "alias_not_allowed"
    if alias.mode not in context.identity.allowed_modes:
        # unreachable with validated config (mode coherence); defense in depth
        return "mode_not_allowed"
    return None
