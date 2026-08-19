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


# --- per-candidate reasoning_effort injection (2026-08-17) ---------------------
# Hindsight cannot send reasoning_effort for this route: it only emits the
# parameter for models NAMED gpt-5/o1/o3, and it addresses the route by the alias
# "cerberus/legacy-recovery". These tests pin the injection contract.

def _target(**kw):
    from cerberus.router.engine import Target

    base = dict(
        provider_id="gemini", credential_id="main", model="gemini-3.6-flash",
        cost_tier="free", base_url="https://example.invalid/v1",
        api_key_env="GEMINI_API_KEY", quota_cooldown_seconds=60,
        transport_cooldown_seconds=30, quota_scope="provider",
    )
    base.update(kw)
    return Target(**base)


def _built_body(target, body):
    import httpx

    from cerberus.egress.client import build_upstream_request

    with httpx.Client() as client:
        request = build_upstream_request(client, target, body, "k")
    import json as _json

    return _json.loads(request.content)


def test_reasoning_effort_absent_leaves_body_untouched():
    """Default behaviour must be byte-identical to before the feature existed."""
    body = {"messages": [{"role": "user", "content": "hi"}]}
    built = _built_body(_target(), body)
    assert "reasoning_effort" not in built


def test_reasoning_effort_injected_when_configured():
    built = _built_body(_target(reasoning_effort="minimal"),
                        {"messages": [{"role": "user", "content": "hi"}]})
    assert built["reasoning_effort"] == "minimal"


def test_caller_value_is_not_silently_overwritten():
    """A caller that states its own budget keeps it unless the route claims priority."""
    built = _built_body(_target(reasoning_effort="minimal"),
                        {"messages": [], "reasoning_effort": "high"})
    assert built["reasoning_effort"] == "high"


def test_route_wins_only_with_explicit_override():
    built = _built_body(_target(reasoning_effort="minimal", reasoning_effort_override=True),
                        {"messages": [], "reasoning_effort": "high"})
    assert built["reasoning_effort"] == "minimal"


def test_effort_is_attributable_in_telemetry():
    assert "reasoning_effort" not in _target().describe()
    assert _target(reasoning_effort="low").describe()["reasoning_effort"] == "low"


def test_telemetry_event_carries_effective_reasoning_effort():
    """describe() alone was insufficient: _event builds RoutingEvent from explicit
    fields, so the injected budget must be threaded there or attribution is lost."""
    from cerberus.telemetry.emitter import RoutingEvent

    fields = RoutingEvent.__dataclass_fields__
    assert "reasoning_effort" in fields
    assert fields["reasoning_effort"].default is None  # absent -> nothing claimed


def test_chat_template_kwargs_absent_leaves_body_untouched():
    """Default must stay byte-identical for every route that does not set it."""
    built = _built_body(_target(), {"messages": [{"role": "user", "content": "hi"}]})
    assert "chat_template_kwargs" not in built


def test_chat_template_kwargs_injected_when_configured():
    """The llama.cpp thinking lever: reasoning_effort is ignored there, this is not."""
    built = _built_body(_target(chat_template_kwargs={"enable_thinking": False}),
                        {"messages": [{"role": "user", "content": "hi"}]})
    assert built["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_template_kwargs_caller_value_is_not_silently_overwritten():
    built = _built_body(_target(chat_template_kwargs={"enable_thinking": False}),
                        {"messages": [], "chat_template_kwargs": {"enable_thinking": True}})
    assert built["chat_template_kwargs"] == {"enable_thinking": True}


def test_chat_template_kwargs_route_wins_only_with_explicit_override():
    built = _built_body(
        _target(chat_template_kwargs={"enable_thinking": False}, chat_template_kwargs_override=True),
        {"messages": [], "chat_template_kwargs": {"enable_thinking": True}})
    assert built["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_template_kwargs_is_attributable_in_telemetry():
    """A thinking-off run and a thinking-on run must be distinguishable after the
    fact, or their latency and quality numbers cannot be compared."""
    assert "chat_template_kwargs" not in _target().describe()
    described = _target(chat_template_kwargs={"enable_thinking": False}).describe()
    assert described["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_template_kwargs_config_mapping_is_not_aliased_into_the_body():
    """Mutating the built body must never reach back into the route's config."""
    configured = {"enable_thinking": False}
    built = _built_body(_target(chat_template_kwargs=configured), {"messages": []})
    built["chat_template_kwargs"]["enable_thinking"] = True
    assert configured == {"enable_thinking": False}


def test_both_levers_are_independent():
    """A route may need reasoning_effort for one backend and chat_template_kwargs
    for another; setting one must never imply or suppress the other."""
    built = _built_body(
        _target(reasoning_effort="minimal", chat_template_kwargs={"enable_thinking": False}),
        {"messages": []})
    assert built["reasoning_effort"] == "minimal"
    assert built["chat_template_kwargs"] == {"enable_thinking": False}
