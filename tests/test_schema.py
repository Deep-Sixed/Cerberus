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
