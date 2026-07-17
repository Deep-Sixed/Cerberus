"""Session 2 acceptance tests — deterministic selection, cost gate, scoped cooldowns."""

import time

from cerberus.registry import CerberusConfig
from cerberus.router.engine import cost_eligible, ordered_targets
from cerberus.state import InMemoryCooldownStore

RAW = {
    "metadata": {"version": "cerberus-2026-07-16.1"},
    "providers": {
        "alpha": {
            "protocol": "openai",
            "base_url": "https://alpha.example/v1",
            "credentials": {"a1": {"api_key_env": "ALPHA_KEY_1"}, "a2": {"api_key_env": "ALPHA_KEY_2"}},
            "models": {
                "alpha-free": {"cost_tier": "free"},
                "alpha-mini": {"cost_tier": "free"},
            },
        },
        "beta": {
            "protocol": "openai",
            "base_url": "https://beta.example/v1",
            "credentials": {"primary": {"api_key_env": "BETA_KEY"}},
            "models": {"beta-pro": {"cost_tier": "paid"}},
        },
    },
    "aliases": {
        "cerberus/free": {
            "mode": "free",
            "candidates": [
                {"provider": "alpha", "credential": "a1", "model": "alpha-free"},
                {"provider": "alpha", "credential": "a2", "model": "alpha-mini"},
            ],
        },
        "cerberus/dispatch-mixed": {
            "mode": "dispatch",
            "allow_paid_fallback": False,
            "candidates": [
                {"provider": "beta", "credential": "primary", "model": "beta-pro"},
                {"provider": "alpha", "credential": "a1", "model": "alpha-free"},
            ],
        },
        "cerberus/dispatch-paid": {
            "mode": "dispatch",
            "candidates": [{"provider": "beta", "credential": "primary", "model": "beta-pro"}],
        },
    },
}


def config() -> CerberusConfig:
    return CerberusConfig.model_validate(RAW)


def test_ordered_targets_follow_declared_order_and_are_deterministic():
    cfg = config()
    first = ordered_targets(cfg, "cerberus/free")
    second = ordered_targets(cfg, "cerberus/free")
    assert [(t.provider_id, t.credential_id, t.model) for t in first] == [
        ("alpha", "a1", "alpha-free"),
        ("alpha", "a2", "alpha-mini"),
    ]
    assert first == second  # same config -> same ordered policy decision


def test_paid_candidate_excluded_when_fallback_prohibited_and_free_exists():
    cfg = config()
    alias = cfg.aliases["cerberus/dispatch-mixed"]
    eligible, exclusions = cost_eligible(alias, ordered_targets(cfg, "cerberus/dispatch-mixed"))
    assert [t.model for t in eligible] == ["alpha-free"]
    assert exclusions[0]["reason"] == "paid_fallback_prohibited"
    assert exclusions[0]["model"] == "beta-pro"


def test_all_paid_alias_is_not_a_fallback_and_remains_eligible():
    cfg = config()
    alias = cfg.aliases["cerberus/dispatch-paid"]
    eligible, exclusions = cost_eligible(alias, ordered_targets(cfg, "cerberus/dispatch-paid"))
    assert [t.model for t in eligible] == ["beta-pro"]
    assert exclusions == []


def test_free_mode_alias_guard_is_defense_in_depth():
    cfg = config()
    alias = cfg.aliases["cerberus/free"]
    targets = ordered_targets(cfg, "cerberus/free")
    # simulate a corrupted target sneaking a paid tier past validation
    poisoned = [targets[0], targets[1].replace_cost_tier("paid")]
    eligible, exclusions = cost_eligible(alias, poisoned)
    assert [t.model for t in eligible] == ["alpha-free"]
    assert exclusions[0]["reason"] == "cost_tier_guard"


def test_model_scoped_cooldown_excludes_only_that_model():
    store = InMemoryCooldownStore()
    store.apply(scope="model", provider="alpha", credential="a1", model="alpha-free", reason="quota_429", duration_seconds=60)
    assert store.active_for("alpha", "a1", "alpha-free") is not None
    assert store.active_for("alpha", "a1", "alpha-mini") is None
    assert store.active_for("alpha", "a2", "alpha-free") is None


def test_credential_scoped_cooldown_excludes_all_models_of_that_credential():
    store = InMemoryCooldownStore()
    store.apply(scope="credential", provider="alpha", credential="a1", model=None, reason="account_rate_limit", duration_seconds=60)
    assert store.active_for("alpha", "a1", "alpha-free") is not None
    assert store.active_for("alpha", "a1", "alpha-mini") is not None
    assert store.active_for("alpha", "a2", "alpha-free") is None


def test_provider_scoped_cooldown_excludes_everything_from_provider():
    store = InMemoryCooldownStore()
    store.apply(scope="provider", provider="alpha", credential=None, model=None, reason="outage", duration_seconds=60)
    assert store.active_for("alpha", "a1", "alpha-free") is not None
    assert store.active_for("alpha", "a2", "alpha-mini") is not None
    assert store.active_for("beta", "primary", "beta-pro") is None


def test_cooldown_expires():
    store = InMemoryCooldownStore()
    store.apply(scope="model", provider="alpha", credential="a1", model="alpha-free", reason="quota_429", duration_seconds=60, now=time.time() - 120)
    assert store.active_for("alpha", "a1", "alpha-free") is None


def test_snapshot_reports_remaining_seconds_and_scope():
    store = InMemoryCooldownStore()
    store.apply(scope="model", provider="alpha", credential="a1", model="alpha-free", reason="quota_429", duration_seconds=60)
    snapshot = store.snapshot()
    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry["scope"] == "model" and entry["reason"] == "quota_429"
    assert 0 < entry["seconds_remaining"] <= 60
