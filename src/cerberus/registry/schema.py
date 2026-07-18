"""Cerberus configuration schema — registry, aliases, identities (SPEC.md caps 1-3).

Self-contained and fail-closed: every cross-reference is resolved at validation
time, cost tiers are registry-derived (restatements are checked, never trusted),
authorization is allow-list only, and unknown keys are rejected everywhere.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

CostTier = Literal["free", "paid"]
AliasMode = Literal["direct", "dispatch", "free", "fusion"]

ALIAS_PREFIX = "cerberus/"
VERSION_PATTERN = r"^cerberus-\d{4}-\d{2}-\d{2}\.\d+$"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ConfigMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=VERSION_PATTERN)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=4000, ge=1, le=65535)
    api_token_env: str | None = None
    # distinct admin scope: the inference token must never authorize config
    # mutation; without this, mutating admin endpoints are loopback-only
    admin_token_env: str | None = None

    @model_validator(mode="after")
    def validate_credential_separation(self) -> "ServerConfig":
        if self.admin_token_env is not None and self.admin_token_env == self.api_token_env:
            raise ValueError("admin_token_env must name a different environment variable than api_token_env")
        return self


class TelemetryConfig(BaseModel):
    """Optional redacted routing-event sink; never on the inference critical path."""

    model_config = ConfigDict(extra="forbid")

    endpoint: HttpUrl | None = None
    bearer_token_file: str | None = None
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    queue_capacity: int = Field(default=256, ge=1, le=4096)

    @model_validator(mode="after")
    def validate_sink(self) -> "TelemetryConfig":
        if (self.endpoint is None) != (self.bearer_token_file is None):
            raise ValueError("telemetry endpoint and bearer_token_file must be configured together")
        return self


class FusionWorkerConfig(BaseModel):
    """Bundled fusion worker binding — fixed at boot, like the telemetry sink.

    The worker executes panel+judge; Cerberus holds the policy and fans out to it.
    Absent this block, fusion-mode aliases have nowhere to run and fail closed.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: HttpUrl | None = None
    bearer_token_env: str | None = None
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_worker(self) -> "FusionWorkerConfig":
        if (self.endpoint is None) != (self.bearer_token_env is None):
            raise ValueError("fusion_worker endpoint and bearer_token_env must be configured together")
        return self


class AuthentikConfig(BaseModel):
    """Authentik is the sole token issuer; Cerberus only validates (asymmetric algs only)."""

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1)
    jwks_url: HttpUrl
    audience: str = Field(min_length=1)
    algorithms: list[Literal["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]] = Field(
        default_factory=lambda: ["RS256", "ES256"], min_length=1
    )
    cache_ttl_seconds: int = Field(default=300, ge=10)


class AdminSSOConfig(BaseModel):
    """Authentik OIDC login for the admin console (Authorization Code + PKCE).

    Authentik is the sole authority: Cerberus starts the flow, validates the
    id_token against Authentik's JWKS, and gates on an admin group claim. The
    browser session is a Cerberus signed cookie (allowed — it is not an
    externally-accepted MCP/API bearer). Break-glass is an Authentik-issued
    admin-scoped bearer in the header, never a Cerberus-minted token.
    """

    model_config = ConfigDict(extra="forbid")

    authorize_url: HttpUrl
    token_url: HttpUrl
    jwks_url: HttpUrl
    issuer: str = Field(min_length=1)
    client_id_env: str = Field(min_length=1)
    client_secret_env: str = Field(min_length=1)
    redirect_uri: HttpUrl
    admin_groups: list[str] = Field(min_length=1)
    session_secret_env: str = Field(min_length=1)
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email", "groups"])
    ca_bundle: str | None = None  # path to trust Authentik's TLS cert; None = system trust
    algorithms: list[Literal["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]] = Field(
        default_factory=lambda: ["RS256", "ES256"], min_length=1
    )


class StateConfig(BaseModel):
    """Persistent runtime state location; null means in-memory (tests, dry runs)."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cost_tier: CostTier
    capabilities: list[str] = Field(default_factory=list)
    context_window: int | None = Field(default=None, ge=1)


class CredentialEntry(BaseModel):
    """A named secret *reference* (env var populated by the file-secrets bridge). Never a value."""

    model_config = ConfigDict(extra="forbid")

    api_key_env: str = Field(min_length=1)


class ProviderEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["openai"] = "openai"
    maturity: Literal["stable", "experimental"] = "stable"
    base_url: HttpUrl
    credentials: dict[str, CredentialEntry] = Field(min_length=1)
    models: dict[str, ModelEntry] = Field(min_length=1)
    quota_cooldown_seconds: int = Field(default=3600, ge=1)
    transport_cooldown_seconds: int = Field(default=30, ge=1)
    # model: a 429 cools only the failing model; credential: account-wide
    # exhaustion, one 429 cools every model under that credential
    quota_scope: Literal["model", "credential"] = "model"


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    credential: str = Field(min_length=1)
    model: str = Field(min_length=1)
    cost_tier: CostTier | None = None  # optional restatement; verified against the registry
    # fusion panels only: a per-seat system prompt (the member's assigned stance).
    # Ignored outside a fusion alias's candidate/judge list.
    role: str | None = Field(default=None, min_length=1)

    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.credential, self.model)


class FusionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_panel_members: int = Field(ge=1, le=8)
    timeout_seconds: float = Field(gt=0, le=600)
    allow_paid_panel: bool = False
    judge: Candidate
    on_partial_failure: Literal["judge_with_partial", "fail"] = "judge_with_partial"
    require_human_review: bool = False


class Alias(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AliasMode
    candidates: list[Candidate] = Field(min_length=1)
    allow_paid_fallback: bool = False
    allow_experimental: bool = False
    fusion: FusionPolicy | None = None

    @model_validator(mode="after")
    def validate_mode_coherence(self) -> "Alias":
        if self.mode == "fusion" and self.fusion is None:
            raise ValueError("fusion-mode alias requires a fusion policy block")
        if self.mode != "fusion" and self.fusion is not None:
            raise ValueError("fusion policy block is only valid on a fusion-mode alias")
        if self.mode == "free" and self.allow_paid_fallback:
            raise ValueError("free-mode alias must not set allow_paid_fallback")
        return self


class Identity(BaseModel):
    """Authorization record. Allow-lists only — anything absent is denied."""

    model_config = ConfigDict(extra="forbid")

    credential_env: str | None = Field(default=None, min_length=1)
    jwt_client_id: str | None = Field(default=None, min_length=1)
    allowed_modes: list[AliasMode] = Field(min_length=1)
    allowed_aliases: list[str] = Field(min_length=1)
    default_alias: str | None = None
    # Opt-in to routing raw provider/model ids directly (bypassing aliases). Off
    # by default: identities keep least privilege to their allowed_aliases; the
    # rich-picker catalog and direct routing are unlocked only where intended.
    allow_direct_models: bool = False

    @model_validator(mode="after")
    def validate_credential_source(self) -> "Identity":
        if (self.credential_env is None) == (self.jwt_client_id is None):
            raise ValueError("identity requires exactly one of credential_env or jwt_client_id")
        return self


class CerberusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: ConfigMetadata
    authentik: AuthentikConfig | None = None
    admin_sso: AdminSSOConfig | None = None
    server: ServerConfig = Field(default_factory=ServerConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    fusion_worker: FusionWorkerConfig = Field(default_factory=FusionWorkerConfig)
    providers: dict[str, ProviderEntry] = Field(min_length=1)
    aliases: dict[str, Alias] = Field(min_length=1)
    identities: dict[str, Identity] = Field(default_factory=dict)

    def resolve_model(self, candidate: Candidate, *, context: str) -> ModelEntry:
        provider = self.providers.get(candidate.provider)
        if provider is None:
            raise ValueError(f"{context}: unknown provider {candidate.provider!r}")
        if candidate.credential not in provider.credentials:
            raise ValueError(f"{context}: unknown credential {candidate.credential!r} for provider {candidate.provider!r}")
        model = provider.models.get(candidate.model)
        if model is None:
            raise ValueError(f"{context}: unknown model {candidate.model!r} for provider {candidate.provider!r}")
        return model

    def _validate_candidate(self, alias_name: str, alias: Alias, candidate: Candidate, *, role: str) -> ModelEntry:
        context = f"alias {alias_name!r} {role}"
        model = self.resolve_model(candidate, context=context)
        if candidate.cost_tier is not None and candidate.cost_tier != model.cost_tier:
            raise ValueError(
                f"{context}: cost_tier restatement {candidate.cost_tier!r} contradicts registry "
                f"({candidate.provider}/{candidate.model} is {model.cost_tier!r})"
            )
        if self.providers[candidate.provider].maturity == "experimental" and not alias.allow_experimental:
            raise ValueError(
                f"{context}: provider {candidate.provider!r} is experimental; "
                f"alias must set allow_experimental after compatibility tests pass"
            )
        return model

    @model_validator(mode="after")
    def validate_references(self) -> "CerberusConfig":
        if self.server.host not in LOOPBACK_HOSTS and not self.server.api_token_env:
            raise ValueError("non-loopback server.host requires server.api_token_env")

        for alias_name, alias in self.aliases.items():
            if not alias_name.startswith(ALIAS_PREFIX):
                raise ValueError(f"alias {alias_name!r} must carry the {ALIAS_PREFIX!r} prefix")

            tiers = [
                self._validate_candidate(alias_name, alias, candidate, role="candidate").cost_tier
                for candidate in alias.candidates
            ]
            if alias.mode == "free" and any(tier == "paid" for tier in tiers):
                raise ValueError(f"alias {alias_name!r} is free-mode but lists a paid candidate")

            if alias.fusion is not None:
                if len(alias.candidates) > alias.fusion.max_panel_members:
                    raise ValueError(
                        f"alias {alias_name!r}: panel of {len(alias.candidates)} exceeds "
                        f"max_panel_members={alias.fusion.max_panel_members}"
                    )
                judge_model = self._validate_candidate(alias_name, alias, alias.fusion.judge, role="judge")
                if not alias.fusion.allow_paid_panel and (
                    any(tier == "paid" for tier in tiers) or judge_model.cost_tier == "paid"
                ):
                    raise ValueError(
                        f"alias {alias_name!r}: paid panel member or judge requires allow_paid_panel"
                    )

        for identity_name, identity in self.identities.items():
            if identity.jwt_client_id is not None and self.authentik is None:
                raise ValueError(
                    f"identity {identity_name!r} uses jwt_client_id but no authentik block is configured"
                )
            allowed_modes = set(identity.allowed_modes)
            for alias_name in identity.allowed_aliases:
                alias = self.aliases.get(alias_name)
                if alias is None:
                    raise ValueError(f"identity {identity_name!r} references unknown alias {alias_name!r}")
                if alias.mode not in allowed_modes:
                    raise ValueError(
                        f"identity {identity_name!r} allows alias {alias_name!r} whose mode "
                        f"{alias.mode!r} is not in allowed_modes (deny-by-default contradiction)"
                    )
            if identity.default_alias is not None and identity.default_alias not in identity.allowed_aliases:
                raise ValueError(
                    f"identity {identity_name!r}: default_alias {identity.default_alias!r} "
                    f"is not in allowed_aliases"
                )
        return self
