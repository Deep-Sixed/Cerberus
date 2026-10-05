"""Cerberus configuration schema — registry, aliases, identities (see docs/architecture.md).

Self-contained and fail-closed: every cross-reference is resolved at validation
time, cost tiers are registry-derived (restatements are checked, never trusted),
authorization is allow-list only, and unknown keys are rejected everywhere.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

CostTier = Literal["free", "paid"]
AliasMode = Literal["direct", "dispatch", "free", "fusion", "jev-router"]
# Managed deliberation backends Cerberus can hand a fusion request to.
FusionBackendName = Literal["openrouter"]
# Managed model-selection routers Cerberus can hand a jev-router request to.
JevRouterBackendName = Literal["openrouter"]
# OpenRouter's jev-router plugin accepts at most this many include patterns.
JEV_ROUTER_MAX_POOL = 1024

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
    # /docs and /openapi.json enumerate the whole admin surface, so they follow the
    # admin boundary by default. Opt out only in development.
    public_docs: bool = False
    # Additional directories /admin/validate, /admin/activate and /admin/shadow
    # may read a candidate configuration from. The staging directory and the
    # directory holding the active configuration are always allowed; this is for
    # a deployment that authors revisions somewhere else. Explicit configuration
    # only — never discovered — and empty by default, so the boundary does not
    # widen unless an operator says so.
    admin_config_roots: list[str] = Field(default_factory=list)
    # Loopback trust (admin access without a credential, and the token-less
    # inference surface) also requires the request's Host to name this machine:
    # localhost, *.localhost, 127.0.0.0/8 or ::1. A DNS-rebinding page reaches
    # 127.0.0.1 from the operator's browser under its own hostname, so the peer
    # address alone cannot tell it from a local client. List any other name
    # local clients use here.
    trusted_hosts: list[str] = Field(default_factory=list)

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
    # Break-glass is deliberately narrower than a login. The token must carry this
    # scope — an id_token never does, so a captured login token cannot be replayed
    # as an admin bearer — and must be short-lived *at issue* (exp - iat), so an
    # Authentik long-lived token can never serve as a permanent admin key.
    break_glass_scope: str = Field(default="cerberus:admin", min_length=1)
    break_glass_max_lifetime_seconds: int = Field(default=3600, ge=60, le=86400)
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
    # How the admin health probe checks this provider. "models" GETs /models;
    # "chat" sends a 1-token completion for providers that do not serve /models
    # over GET (e.g. Cloudflare Workers AI returns "GET not supported" there,
    # which would otherwise render as a false failure in the console).
    health_probe: Literal["models", "chat"] = "models"


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    credential: str = Field(min_length=1)
    model: str = Field(min_length=1)
    cost_tier: CostTier | None = None  # optional restatement; verified against the registry
    # Optional per-candidate reasoning budget, injected into the upstream body.
    # Absent means "send nothing": the provider's own default applies and
    # behaviour is identical to before this field existed.
    #
    # Why it lives here: Hindsight only emits reasoning_effort for models whose
    # NAME matches gpt-5/o1/o3, and it addresses this route by the alias
    # "cerberus/legacy-recovery", so the parameter never reaches Gemini.
    # Measured 2026-08-17 on ONE extraction-shaped prompt: default spent 772
    # thinking tokens against 136 visible output at 6.6s, while minimal and low
    # reported none at ~1.5s. Google bills thinking as output, so this is a real
    # cost and latency lever — but a single observation must not be generalised
    # to a whole corpus.
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    # A caller that sets reasoning_effort itself keeps it unless the route
    # explicitly claims precedence. Silently overwriting a caller's stated
    # reasoning budget would hide the substitution from whoever asked for it.
    reasoning_effort_override: bool = False
    # Optional per-candidate chat-template arguments, injected into the upstream
    # body. Absent means "send nothing", exactly as with reasoning_effort.
    #
    # Why this exists alongside reasoning_effort rather than reusing it: they are
    # not interchangeable. llama.cpp IGNORES reasoning_effort outright — measured
    # 2026-08-19 against gemma-4 E2B, where minimal produced byte-identical output
    # and the same 524 completion tokens as the default. The lever llama.cpp does
    # honour is chat_template_kwargs {"enable_thinking": false}, measured on
    # gemma-4 12B (Thanatos) at 4.1s/184 tokens against 20.1s/929 tokens with
    # thinking on — a ~5x latency difference across a 2492-document corpus.
    # As with reasoning_effort, one prompt is not a corpus.
    chat_template_kwargs: dict[str, Any] | None = None
    # Same precedence rule as reasoning_effort: a caller that states its own
    # chat_template_kwargs keeps them unless the route explicitly claims
    # precedence, so a substitution is never silent.
    chat_template_kwargs_override: bool = False

    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.credential, self.model)


class FusionPolicy(BaseModel):
    """Policy for a fusion-mode alias.

    Cerberus decides WHETHER a request is a fusion request and WHICH models take
    part; a managed backend performs the deliberation. Three participants exist
    and every one is a registry-validated candidate under this policy's cost
    rule: the panel (``candidates``), the analyst that compares the panel's
    answers (``judge``; OpenRouter Fusion calls this the analysis ``model``), and
    the outer model that receives the analysis and writes the final answer
    (``outer``, defaulting to the judge). Leaving the outer model to the backend
    would let an unregistered, possibly paid model take part.
    """

    model_config = ConfigDict(extra="forbid")

    backend: FusionBackendName = "openrouter"
    # OpenRouter Fusion accepts 1-8 analysis models; the schema cap matches.
    max_panel_members: int = Field(ge=1, le=8)
    # Absolute deadline for the whole deliberation, enforced by Cerberus.
    timeout_seconds: float = Field(gt=0, le=600)
    allow_paid_panel: bool = False
    judge: Candidate
    outer: Candidate | None = None
    require_human_review: bool = False

    @property
    def outer_model(self) -> Candidate:
        return self.outer if self.outer is not None else self.judge


class JevRouterPolicy(BaseModel):
    """Policy for a jev-router alias: Jev as one model-selection strategy.

    Cerberus decides WHICH models may serve the alias: the candidates, each a
    registry-validated exact slug, filtered before every request by cost policy,
    provider health, cooldowns and credential presence. Only what survives is
    handed to OpenRouter's hosted Jev Router, which picks one model (and its
    reasoning effort) from that pool. Jev chooses within Cerberus policy; it
    never widens it.

    This is the hosted-router strategy. It can only choose among models one
    OpenRouter credential reaches, so every candidate shares a provider and
    credential, exactly as a fusion panel does.
    """

    model_config = ConfigDict(extra="forbid")

    backend: JevRouterBackendName = "openrouter"
    # Absolute deadline for the routed request, enforced by Cerberus.
    timeout_seconds: float = Field(gt=0, le=600)
    # Jev picks the cheapest model strong enough for the request, which is often
    # a paid one, and may add an advisor on the hardest requests. A pool with any
    # paid model is therefore an explicit opt-in, as allow_paid_panel is.
    allow_paid_pool: bool = False


class Alias(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AliasMode
    candidates: list[Candidate] = Field(min_length=1)
    allow_paid_fallback: bool = False
    allow_experimental: bool = False
    fusion: FusionPolicy | None = None
    jev_router: JevRouterPolicy | None = None

    @model_validator(mode="after")
    def validate_mode_coherence(self) -> "Alias":
        if self.mode == "fusion" and self.fusion is None:
            raise ValueError("fusion-mode alias requires a fusion policy block")
        if self.mode != "fusion" and self.fusion is not None:
            raise ValueError("fusion policy block is only valid on a fusion-mode alias")
        if self.mode == "jev-router" and self.jev_router is None:
            raise ValueError("jev-router alias requires a jev_router policy block")
        if self.mode != "jev-router" and self.jev_router is not None:
            raise ValueError("jev_router policy block is only valid on a jev-router alias")
        if self.mode == "jev-router" and self.allow_paid_fallback:
            # there is no ordered fallback for it to govern: Jev chooses from the
            # pool, and paid membership is jev_router.allow_paid_pool
            raise ValueError("jev-router alias must not set allow_paid_fallback; use jev_router.allow_paid_pool")
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

    def _validate_jev_router_pool(self, alias_name: str, alias: Alias, tiers: list[CostTier]) -> None:
        assert alias.jev_router is not None
        policy = alias.jev_router
        if len(alias.candidates) > JEV_ROUTER_MAX_POOL:
            raise ValueError(
                f"alias {alias_name!r}: pool of {len(alias.candidates)} exceeds the jev-router limit "
                f"of {JEV_ROUTER_MAX_POOL} models"
            )
        anchor = alias.candidates[0]
        seen: set[str] = set()
        for candidate in alias.candidates:
            # One routed request is one provider call under one credential, so the
            # whole pool must be reachable through the same one.
            if (candidate.provider, candidate.credential) != (anchor.provider, anchor.credential):
                raise ValueError(
                    f"alias {alias_name!r}: jev-router backend {policy.backend!r} requires every candidate "
                    f"to use one provider/credential ({anchor.provider}/{anchor.credential}); "
                    f"{candidate.provider}/{candidate.model} uses {candidate.provider}/{candidate.credential}"
                )
            # The plugin reads its include list as patterns: "*" is a wildcard and
            # a leading "~" names a whole model family. Either could admit a model
            # the registry never listed, so only exact slugs may enter the pool.
            if "*" in candidate.model or candidate.model.startswith("~"):
                raise ValueError(
                    f"alias {alias_name!r}: jev-router candidate {candidate.model!r} is a pattern; "
                    f"the pool admits exact registry slugs only"
                )
            # Jev sets the reasoning effort for each request. A per-candidate
            # budget would never be applied, so it is refused rather than ignored.
            if candidate.reasoning_effort is not None or candidate.chat_template_kwargs is not None:
                raise ValueError(
                    f"alias {alias_name!r}: jev-router candidate {candidate.model!r} sets a reasoning budget; "
                    f"Jev chooses reasoning effort per request, so it would never be applied"
                )
            if candidate.model in seen:
                raise ValueError(f"alias {alias_name!r}: jev-router candidate {candidate.model!r} is listed twice")
            seen.add(candidate.model)
        if not policy.allow_paid_pool and any(tier == "paid" for tier in tiers):
            raise ValueError(f"alias {alias_name!r}: paid jev-router pool member requires allow_paid_pool")

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
                outer_model = self._validate_candidate(alias_name, alias, alias.fusion.outer_model, role="outer")
                # A managed backend runs the whole deliberation as ONE provider call
                # under ONE credential, so every participant must be reachable
                # through the judge's.
                judge = alias.fusion.judge
                for label, candidate in [("panel", c) for c in alias.candidates] + [("outer", alias.fusion.outer_model)]:
                    if (candidate.provider, candidate.credential) != (judge.provider, judge.credential):
                        raise ValueError(
                            f"alias {alias_name!r}: fusion backend {alias.fusion.backend!r} requires every "
                            f"{label} candidate to use the judge's provider/credential "
                            f"({judge.provider}/{judge.credential}); "
                            f"{candidate.provider}/{candidate.model} uses {candidate.credential!r}"
                        )
                if not alias.fusion.allow_paid_panel and (
                    any(tier == "paid" for tier in tiers)
                    or judge_model.cost_tier == "paid"
                    or outer_model.cost_tier == "paid"
                ):
                    raise ValueError(
                        f"alias {alias_name!r}: paid panel member, judge or outer model requires allow_paid_panel"
                    )

            if alias.jev_router is not None:
                self._validate_jev_router_pool(alias_name, alias, tiers)

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
