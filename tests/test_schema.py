"""Session 1 acceptance tests — Cerberus config schema (registry, aliases and identity validation)."""

import pytest
from pydantic import ValidationError

from cerberus.registry import CerberusConfig

BASE: dict = {
    "metadata": {"version": "cerberus-2026-07-16.1"},
    "server": {"host": "127.0.0.1", "port": 4000},
    "providers": {
        "openrouter": {
            "protocol": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "credentials": {"primary": {"api_key_env": "OPENROUTER_API_KEY"}},
            "models": {
                "openrouter/free": {"cost_tier": "free", "capabilities": ["chat", "tools"]},
                "anthropic/claude-sonnet": {"cost_tier": "paid", "capabilities": ["chat", "tools", "coding"]},
            },
        },
        "google": {
            "protocol": "openai",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "credentials": {
                "free-1": {"api_key_env": "GEMINI_KEY_1"},
                "free-2": {"api_key_env": "GEMINI_KEY_2"},
            },
            "models": {"gemini-flash": {"cost_tier": "free"}},
        },
    },
    "aliases": {
        "cerberus/free": {
            "mode": "free",
            "candidates": [
                {"provider": "google", "credential": "free-1", "model": "gemini-flash"},
                {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
            ],
        },
        "cerberus/dispatch-ledger": {
            "mode": "dispatch",
            "allow_paid_fallback": True,
            "candidates": [
                {"provider": "openrouter", "credential": "primary", "model": "anthropic/claude-sonnet"},
            ],
        },
    },
    "identities": {
        "recon": {
            "credential_env": "CB_KEY_RECON",
            "allowed_modes": ["free"],
            "allowed_aliases": ["cerberus/free"],
        },
    },
}


def make(**overrides) -> dict:
    import copy

    raw = copy.deepcopy(BASE)
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if value is ...:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
    return raw


def test_valid_config_round_trips():
    config = CerberusConfig.model_validate(BASE)
    assert config.metadata.version == "cerberus-2026-07-16.1"
    assert config.aliases["cerberus/free"].mode == "free"
    assert list(config.providers["google"].credentials) == ["free-1", "free-2"]


def test_version_format_enforced():
    with pytest.raises(ValidationError, match="version"):
        CerberusConfig.model_validate(make(**{"metadata.version": "v4-final"}))


def test_unknown_provider_in_candidate_rejected():
    raw = make()
    raw["aliases"]["cerberus/free"]["candidates"][0]["provider"] = "ghost"
    with pytest.raises(ValidationError, match="unknown provider"):
        CerberusConfig.model_validate(raw)


def test_unknown_credential_in_candidate_rejected():
    raw = make()
    raw["aliases"]["cerberus/free"]["candidates"][0]["credential"] = "ghost"
    with pytest.raises(ValidationError, match="unknown credential"):
        CerberusConfig.model_validate(raw)


def test_unknown_model_in_candidate_rejected():
    raw = make()
    raw["aliases"]["cerberus/free"]["candidates"][0]["model"] = "ghost"
    with pytest.raises(ValidationError, match="unknown model"):
        CerberusConfig.model_validate(raw)


def test_free_alias_with_paid_candidate_rejected():
    raw = make()
    raw["aliases"]["cerberus/free"]["candidates"].append(
        {"provider": "openrouter", "credential": "primary", "model": "anthropic/claude-sonnet"}
    )
    with pytest.raises(ValidationError, match="free"):
        CerberusConfig.model_validate(raw)


def test_free_alias_cannot_allow_paid_fallback():
    raw = make()
    raw["aliases"]["cerberus/free"]["allow_paid_fallback"] = True
    with pytest.raises(ValidationError, match="allow_paid_fallback"):
        CerberusConfig.model_validate(raw)


def test_candidate_cost_tier_restatement_must_match_registry():
    raw = make()
    raw["aliases"]["cerberus/free"]["candidates"][0]["cost_tier"] = "paid"
    with pytest.raises(ValidationError, match="cost_tier"):
        CerberusConfig.model_validate(raw)
    raw["aliases"]["cerberus/free"]["candidates"][0]["cost_tier"] = "free"
    assert CerberusConfig.model_validate(raw)


def test_alias_names_must_carry_cerberus_prefix():
    raw = make()
    raw["aliases"]["legacy/free"] = raw["aliases"].pop("cerberus/free")
    raw["identities"]["recon"]["allowed_aliases"] = ["legacy/free"]
    with pytest.raises(ValidationError, match="cerberus/"):
        CerberusConfig.model_validate(raw)


def test_experimental_provider_requires_alias_opt_in():
    raw = make(
        **{
            "providers.anthropic-compat": {
                "protocol": "openai",
                "maturity": "experimental",
                "base_url": "https://api.anthropic.com/v1",
                "credentials": {"primary": {"api_key_env": "ANTHROPIC_API_KEY"}},
                "models": {"claude-sonnet": {"cost_tier": "paid"}},
            }
        }
    )
    raw["aliases"]["cerberus/dispatch-ledger"]["candidates"].append(
        {"provider": "anthropic-compat", "credential": "primary", "model": "claude-sonnet"}
    )
    with pytest.raises(ValidationError, match="experimental"):
        CerberusConfig.model_validate(raw)
    raw["aliases"]["cerberus/dispatch-ledger"]["allow_experimental"] = True
    assert CerberusConfig.model_validate(raw)


def test_identity_unknown_alias_rejected():
    raw = make()
    raw["identities"]["recon"]["allowed_aliases"] = ["cerberus/ghost"]
    with pytest.raises(ValidationError, match="unknown alias"):
        CerberusConfig.model_validate(raw)


def test_identity_mode_coherence_enforced():
    """An identity allowed an alias whose mode it is not allowed is a contradiction."""
    raw = make()
    raw["identities"]["recon"]["allowed_aliases"] = ["cerberus/free", "cerberus/dispatch-ledger"]
    # allowed_modes is ["free"] — dispatch alias must be rejected
    with pytest.raises(ValidationError, match="mode"):
        CerberusConfig.model_validate(raw)


def test_identity_deny_lists_do_not_exist():
    raw = make()
    raw["identities"]["recon"]["denied_modes"] = ["fusion"]
    with pytest.raises(ValidationError):
        CerberusConfig.model_validate(raw)


def test_identity_default_alias_must_be_allowed():
    raw = make()
    raw["identities"]["recon"]["default_alias"] = "cerberus/dispatch-ledger"
    with pytest.raises(ValidationError, match="default_alias"):
        CerberusConfig.model_validate(raw)


def test_fusion_alias_requires_policy_block():
    raw = make(
        **{
            "aliases.cerberus/fusion-code": {
                "mode": "fusion",
                "candidates": [
                    {"provider": "google", "credential": "free-1", "model": "gemini-flash"},
                    {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
                ],
            }
        }
    )
    with pytest.raises(ValidationError, match="fusion"):
        CerberusConfig.model_validate(raw)


def _fusion_raw(**alias_overrides) -> dict:
    alias = {
        "mode": "fusion",
        "candidates": [
            {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
            {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
        ],
        "fusion": {
            "max_panel_members": 5,
            "timeout_seconds": 180,
            "judge": {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
        },
    }
    alias.update(alias_overrides)
    return make(**{"aliases.cerberus/fusion-code": alias})


def test_fusion_alias_valid():
    config = CerberusConfig.model_validate(_fusion_raw())
    fusion = config.aliases["cerberus/fusion-code"].fusion
    assert fusion is not None and fusion.allow_paid_panel is False
    assert fusion.backend == "openrouter"  # the default managed backend


def test_fusion_panel_must_share_the_judges_provider_and_credential():
    """A managed backend runs the panel in ONE upstream call under ONE key, so a
    seat on another provider (or credential) can never be honored — reject it."""
    raw = _fusion_raw()
    raw["aliases"]["cerberus/fusion-code"]["candidates"][0] = {
        "provider": "google", "credential": "free-1", "model": "gemini-flash"
    }
    with pytest.raises(ValidationError, match="judge's provider/credential"):
        CerberusConfig.model_validate(raw)


def test_fusion_rejects_unknown_backend_and_removed_worker_fields():
    raw = _fusion_raw()
    raw["aliases"]["cerberus/fusion-code"]["fusion"]["backend"] = "bundled-worker"
    with pytest.raises(ValidationError, match="backend"):
        CerberusConfig.model_validate(raw)
    # the bundled-worker binding and per-seat role prompts are gone; a stale
    # config must fail loudly rather than be silently accepted without effect
    for stale in ({"fusion_worker": {"endpoint": "http://w:1", "bearer_token_env": "T"}},):
        with pytest.raises(ValidationError, match="fusion_worker"):
            CerberusConfig.model_validate({**_fusion_raw(), **stale})
    raw = _fusion_raw()
    raw["aliases"]["cerberus/fusion-code"]["candidates"][0]["role"] = "Skeptic."
    with pytest.raises(ValidationError, match="role"):
        CerberusConfig.model_validate(raw)


def test_fusion_policy_on_non_fusion_alias_rejected():
    raw = make()
    raw["aliases"]["cerberus/free"]["fusion"] = {
        "max_panel_members": 3,
        "timeout_seconds": 60,
        "judge": {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
    }
    with pytest.raises(ValidationError, match="fusion"):
        CerberusConfig.model_validate(raw)


def test_fusion_panel_cannot_exceed_max_members():
    raw = _fusion_raw()
    raw["aliases"]["cerberus/fusion-code"]["fusion"]["max_panel_members"] = 1
    with pytest.raises(ValidationError, match="panel"):
        CerberusConfig.model_validate(raw)


def test_fusion_judge_must_resolve_in_registry():
    raw = _fusion_raw()
    raw["aliases"]["cerberus/fusion-code"]["fusion"]["judge"]["model"] = "ghost"
    with pytest.raises(ValidationError, match="unknown model"):
        CerberusConfig.model_validate(raw)


def test_fusion_free_panel_enforced_unless_paid_allowed():
    raw = _fusion_raw()
    raw["aliases"]["cerberus/fusion-code"]["candidates"].append(
        {"provider": "openrouter", "credential": "primary", "model": "anthropic/claude-sonnet"}
    )
    with pytest.raises(ValidationError, match="paid"):
        CerberusConfig.model_validate(raw)
    raw["aliases"]["cerberus/fusion-code"]["fusion"]["allow_paid_panel"] = True
    assert CerberusConfig.model_validate(raw)


def test_non_loopback_server_requires_api_token_env():
    raw = make(**{"server.host": "0.0.0.0"})
    with pytest.raises(ValidationError, match="api_token_env"):
        CerberusConfig.model_validate(raw)


def test_unknown_top_level_keys_rejected():
    with pytest.raises(ValidationError):
        CerberusConfig.model_validate(make(pools={"free": {"providers": ["openrouter"]}}))


# -- jev-router: Jev chooses within a pool Cerberus validates ------------------


def _jev_raw(**alias_overrides) -> dict:
    alias = {
        "mode": "jev-router",
        "candidates": [
            {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
        ],
        "jev_router": {"timeout_seconds": 60},
    }
    alias.update(alias_overrides)
    return make(**{"aliases.cerberus/auto": alias})


def test_jev_router_alias_valid():
    config = CerberusConfig.model_validate(_jev_raw())
    policy = config.aliases["cerberus/auto"].jev_router
    assert policy is not None and policy.backend == "openrouter" and policy.allow_paid_pool is False


def test_jev_router_mode_and_policy_block_must_agree():
    with pytest.raises(ValidationError, match="jev_router policy block"):
        CerberusConfig.model_validate(_jev_raw(jev_router=None))
    raw = make()
    raw["aliases"]["cerberus/free"]["jev_router"] = {"timeout_seconds": 60}
    with pytest.raises(ValidationError, match="only valid on a jev-router alias"):
        CerberusConfig.model_validate(raw)


def test_jev_router_refuses_allow_paid_fallback():
    with pytest.raises(ValidationError, match="allow_paid_pool"):
        CerberusConfig.model_validate(_jev_raw(allow_paid_fallback=True))


def test_jev_router_paid_pool_member_requires_opt_in():
    paid = [
        {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
        {"provider": "openrouter", "credential": "primary", "model": "anthropic/claude-sonnet"},
    ]
    with pytest.raises(ValidationError, match="allow_paid_pool"):
        CerberusConfig.model_validate(_jev_raw(candidates=paid))
    assert CerberusConfig.model_validate(
        _jev_raw(candidates=paid, jev_router={"timeout_seconds": 60, "allow_paid_pool": True})
    )


def test_jev_router_pool_shares_one_provider_and_credential():
    mixed = [
        {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
        {"provider": "google", "credential": "free-1", "model": "gemini-flash"},
    ]
    with pytest.raises(ValidationError, match="one provider/credential"):
        CerberusConfig.model_validate(_jev_raw(candidates=mixed))


@pytest.mark.parametrize("pattern", ["anthropic/*", "*flash*", "~openai/gpt-luna-latest"])
def test_jev_router_pool_admits_exact_slugs_only(pattern):
    """The plugin reads its list as patterns; a registry key shaped like one
    would admit models the registry never listed."""
    raw = _jev_raw(candidates=[{"provider": "openrouter", "credential": "primary", "model": pattern}])
    raw["providers"]["openrouter"]["models"][pattern] = {"cost_tier": "free"}
    with pytest.raises(ValidationError, match="exact registry slugs"):
        CerberusConfig.model_validate(raw)


@pytest.mark.parametrize(
    "budget", [{"reasoning_effort": "low"}, {"chat_template_kwargs": {"enable_thinking": False}}]
)
def test_jev_router_refuses_a_per_candidate_reasoning_budget(budget):
    candidate = {"provider": "openrouter", "credential": "primary", "model": "openrouter/free", **budget}
    with pytest.raises(ValidationError, match="reasoning effort per request"):
        CerberusConfig.model_validate(_jev_raw(candidates=[candidate]))


def test_jev_router_refuses_a_duplicate_pool_member():
    twice = [{"provider": "openrouter", "credential": "primary", "model": "openrouter/free"}] * 2
    with pytest.raises(ValidationError, match="listed twice"):
        CerberusConfig.model_validate(_jev_raw(candidates=twice))


def test_jev_router_identity_must_allow_the_mode():
    raw = _jev_raw()
    raw["identities"]["recon"]["allowed_aliases"] = ["cerberus/free", "cerberus/auto"]
    with pytest.raises(ValidationError, match="mode"):
        CerberusConfig.model_validate(raw)
    raw["identities"]["recon"]["allowed_modes"] = ["free", "jev-router"]
    assert CerberusConfig.model_validate(raw)


# -- jev: Cerberus asks, Jev decides, Cerberus executes -------------------------


def _jev_native_raw(**alias_overrides) -> dict:
    raw = make()
    raw["providers"]["openrouter"]["models"]["openrouter/free"]["strength"] = "standard"
    raw["providers"]["openrouter"]["models"]["anthropic/claude-sonnet"]["strength"] = "frontier"
    raw["providers"]["google"]["models"]["gemini-flash"]["strength"] = "standard"
    alias = {
        "mode": "jev",
        "candidates": [
            {"provider": "google", "credential": "free-1", "model": "gemini-flash"},
            {"provider": "openrouter", "credential": "primary", "model": "openrouter/free"},
        ],
        "jev": {"decider": {"provider": "openrouter", "credential": "primary"}},
    }
    alias.update(alias_overrides)
    raw["aliases"]["cerberus/smart"] = alias
    return raw


def test_jev_alias_valid_with_pinned_defaults():
    """Unlike jev-router, the pool may span providers and credentials."""
    config = CerberusConfig.model_validate(_jev_native_raw())
    policy = config.aliases["cerberus/smart"].jev
    assert policy is not None
    assert policy.model == "typesafe/jev-1.13"
    assert str(policy.endpoint) == "https://openrouter.ai/api/alpha/decisions"
    assert (policy.input, policy.max_input_chars, policy.allow_paid_pool) == ("last_user_message", 4000, False)


def test_jev_mode_and_policy_block_must_agree():
    with pytest.raises(ValidationError, match="jev policy block"):
        CerberusConfig.model_validate(_jev_native_raw(jev=None))
    raw = _jev_native_raw()
    raw["aliases"]["cerberus/free"]["jev"] = {"decider": {"provider": "openrouter", "credential": "primary"}}
    with pytest.raises(ValidationError, match="only valid on a jev alias"):
        CerberusConfig.model_validate(raw)


def test_jev_decider_must_be_a_registered_credential():
    raw = _jev_native_raw(jev={"decider": {"provider": "openrouter", "credential": "ghost"}})
    with pytest.raises(ValidationError, match="registered provider credential"):
        CerberusConfig.model_validate(raw)


def test_jev_pool_models_need_a_strength():
    raw = _jev_native_raw()
    del raw["providers"]["google"]["models"]["gemini-flash"]["strength"]
    with pytest.raises(ValidationError, match="needs a registry strength"):
        CerberusConfig.model_validate(raw)


def test_jev_paid_pool_member_requires_opt_in_and_refuses_paid_fallback():
    paid = {"provider": "openrouter", "credential": "primary", "model": "anthropic/claude-sonnet"}
    raw = _jev_native_raw()
    raw["aliases"]["cerberus/smart"]["candidates"].append(paid)
    with pytest.raises(ValidationError, match="allow_paid_pool"):
        CerberusConfig.model_validate(raw)
    raw["aliases"]["cerberus/smart"]["jev"]["allow_paid_pool"] = True
    assert CerberusConfig.model_validate(raw)
    raw["aliases"]["cerberus/smart"]["allow_paid_fallback"] = True
    with pytest.raises(ValidationError, match="allow_paid_pool"):
        CerberusConfig.model_validate(raw)


def test_jev_endpoint_must_be_https():
    raw = _jev_native_raw(jev={
        "decider": {"provider": "openrouter", "credential": "primary"},
        "endpoint": "http://decisions.example/api/alpha/decisions",
    })
    with pytest.raises(ValidationError, match="https"):
        CerberusConfig.model_validate(raw)


def test_jev_input_policy_is_a_closed_vocabulary():
    raw = _jev_native_raw(jev={"decider": {"provider": "openrouter", "credential": "primary"}, "input": "everything"})
    with pytest.raises(ValidationError):
        CerberusConfig.model_validate(raw)


def test_jev_reasoning_efforts_need_a_choice_and_a_route_to_apply_to():
    raw = _jev_native_raw()
    policy = raw["aliases"]["cerberus/smart"]["jev"]
    policy["reasoning_efforts"] = ["low", "high"]
    with pytest.raises(ValidationError, match="would never be applied"):
        CerberusConfig.model_validate(raw)
    raw["aliases"]["cerberus/smart"]["candidates"][0]["reasoning_effort"] = "low"
    assert CerberusConfig.model_validate(raw).aliases["cerberus/smart"].jev.reasoning_efforts == ["low", "high"]
    for bad, message in ((["low"], "at least two"), (["low", "low"], "twice"), (["low", "extreme"], "literal_error")):
        policy["reasoning_efforts"] = bad
        with pytest.raises(ValidationError, match=message):
            CerberusConfig.model_validate(raw)
