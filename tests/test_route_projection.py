"""GET /admin/routes — the console's route projection.

Every assertion here exists to stop the same class of bug: the console showing a
route state the router would not actually produce. So the projection is compared
against the real thing wherever it can be — a live dispatch's own exclusions, the
cooldown store's own precedence walk, the authorization predicate itself — rather
than against constants restated in the test.
"""

from __future__ import annotations


import httpx
import pytest
import yaml

from cerberus.app import create_app
from cerberus.fusion.dispatch import fusion_readiness
from cerberus.identity.auth import IdentityContext, authorization_error
from cerberus.registry.loader import load_config_document

CSRF = {"x-cerberus-csrf": "1"}
LOOPBACK = ("127.0.0.1", 40001)

SECRET = "alpha-secret-value"


def raw_config(state_path: str, *, identities: dict | None = None) -> dict:
    """free first, free second, paid third — with paid fallback prohibited."""

    return {
        "metadata": {"version": "cerberus-2026-09-17.1"},
        "state": {"path": state_path},
        "telemetry": {},
        "providers": {
            "alpha": {
                "base_url": "https://alpha.test/v1",
                "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                "models": {
                    "a-free": {"cost_tier": "free"},
                    "a-second": {"cost_tier": "free"},
                    "a-paid": {"cost_tier": "paid"},
                },
            },
            "beta": {
                "base_url": "https://beta.test/v1",
                "credentials": {"main": {"api_key_env": "BETA_KEY"}},
                "models": {"b-free": {"cost_tier": "free"}},
            },
        },
        "aliases": {
            "cerberus/mixed": {
                "mode": "dispatch",
                "allow_paid_fallback": False,
                "candidates": [
                    {"provider": "alpha", "credential": "main", "model": "a-free"},
                    {"provider": "beta", "credential": "main", "model": "b-free"},
                    {"provider": "alpha", "credential": "main", "model": "a-paid"},
                ],
            },
            "cerberus/fusion-review": {
                "mode": "fusion",
                "candidates": [
                    {"provider": "alpha", "credential": "main", "model": "a-free"},
                    {"provider": "alpha", "credential": "main", "model": "a-second"},
                ],
                "fusion": {
                    "max_panel_members": 2,
                    "timeout_seconds": 30,
                    # `outer` deliberately omitted: it must resolve to the judge
                    "judge": {"provider": "alpha", "credential": "main", "model": "a-second"},
                },
            },
        },
        **({"identities": identities} if identities else {}),
    }


def upstream_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", SECRET)
    monkeypatch.setenv("BETA_KEY", "beta-secret-value")


def build(tmp_path, raw, *, transport=None):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return create_app(
        load_config_document(str(path)),
        http_transport=transport or httpx.MockTransport(upstream_ok),
    )


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=LOOPBACK), base_url="http://test"
    )


async def projection(client) -> dict:
    response = await client.get("/admin/routes")
    assert response.status_code == 200, response.text
    return response.json()


def alias_of(payload: dict, name: str) -> dict:
    return next(entry for entry in payload["aliases"] if entry["alias"] == name)


def path_for(entry: dict, model: str) -> dict:
    return next(p for p in entry["paths"] if p["model"] == model)


async def dispatch_exclusions(client, alias: str = "cerberus/mixed") -> dict[str, dict]:
    """Run a real request and return the router's own exclusions, keyed by model."""

    body = {"model": alias, "messages": [{"role": "user", "content": "hi"}]}
    await client.post("/v1/chat/completions", json=body)
    events = (await client.get("/admin/events")).json()["events"]
    assert events, "expected the request to emit a routing event"
    return {item["model"]: item for item in events[-1]["exclusions"]}


# -- policy ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paid_fallback_prohibition_matches_real_dispatch(env, tmp_path):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            router_said = await dispatch_exclusions(client)
            shown = path_for(alias_of(await projection(client), "cerberus/mixed"), "a-paid")

    assert router_said["a-paid"]["reason"] == "paid_fallback_prohibited"
    assert shown["state"] == "excluded"
    assert shown["exclusion"]["reason"] == router_said["a-paid"]["reason"]


@pytest.mark.asyncio
async def test_first_attemptable_is_eligible_and_later_ones_are_standby(env, tmp_path):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            entry = alias_of(await projection(client), "cerberus/mixed")

    assert [p["state"] for p in entry["paths"]] == ["eligible", "standby", "excluded"]
    assert [p["ordinal"] for p in entry["paths"]] == [0, 1, 2]
    assert path_for(entry, "a-free")["exclusion"] is None


# -- runtime gates -----------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_down_matches_real_dispatch(env, tmp_path):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            app.state.control_plane.set_provider_health("alpha", "down", ttl_seconds=60)
            router_said = await dispatch_exclusions(client)
            entry = alias_of(await projection(client), "cerberus/mixed")

    assert router_said["a-free"]["reason"] == "provider_down"
    shown = path_for(entry, "a-free")
    assert shown["state"] == "excluded"
    assert shown["exclusion"]["reason"] == "provider_down"
    # health removed the first preference, so the second becomes the eligible one
    assert path_for(entry, "b-free")["state"] == "eligible"


@pytest.mark.asyncio
async def test_missing_credentials_matches_real_dispatch(env, tmp_path, monkeypatch):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    monkeypatch.setenv("ALPHA_KEY", "")
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            router_said = await dispatch_exclusions(client)
            shown = path_for(alias_of(await projection(client), "cerberus/mixed"), "a-free")

    assert router_said["a-free"]["reason"] == "missing_credentials"
    assert shown["exclusion"]["reason"] == "missing_credentials"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "credential", "model"),
    [("model", "main", "a-free"), ("credential", "main", None), ("provider", None, None)],
)
async def test_cooldown_precedence_matches_active_for(env, tmp_path, scope, credential, model):
    """Whatever active_for() would return for a target is what the screen shows."""

    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            app.state.cooldowns.apply(
                scope=scope, provider="alpha", credential=credential, model=model,
                reason="quota_429", duration_seconds=3600,
            )
            winning = app.state.cooldowns.active_for("alpha", "main", "a-free")
            shown = path_for(alias_of(await projection(client), "cerberus/mixed"), "a-free")

    assert winning is not None
    assert shown["state"] == "excluded"
    assert shown["exclusion"]["reason"] == f"cooldown_{winning.reason}"
    assert shown["exclusion"]["scope"] == winning.scope
    assert shown["exclusion"]["retry_at"] == pytest.approx(winning.retry_at)


@pytest.mark.asyncio
async def test_most_specific_cooldown_wins_when_several_are_active(env, tmp_path):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            store = app.state.cooldowns
            store.apply(scope="provider", provider="alpha", credential=None, model=None,
                        reason="transport_error", duration_seconds=3600)
            store.apply(scope="model", provider="alpha", credential="main", model="a-free",
                        reason="quota_429", duration_seconds=3600)
            winning = store.active_for("alpha", "main", "a-free")
            shown = path_for(alias_of(await projection(client), "cerberus/mixed"), "a-free")

    assert winning.scope == "model"  # most specific, per active_for's own walk
    assert shown["exclusion"]["scope"] == "model"
    assert shown["exclusion"]["reason"] == "cooldown_quota_429"


@pytest.mark.asyncio
async def test_a_target_failing_several_gates_reports_only_the_first(env, tmp_path):
    """A policy-prohibited paid path that is also cooled down still reports the
    prohibition: dispatch never reaches the cooldown check for it."""

    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            app.state.cooldowns.apply(
                scope="model", provider="alpha", credential="main", model="a-paid",
                reason="quota_429", duration_seconds=3600,
            )
            shown = path_for(alias_of(await projection(client), "cerberus/mixed"), "a-paid")

    assert shown["exclusion"]["reason"] == "paid_fallback_prohibited"
    assert shown["exclusion"]["scope"] is None
    assert shown["exclusion"]["retry_at"] is None


# -- identity ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_rows_equal_the_authorization_predicate(env, tmp_path):
    identities = {
        "coding-agent": {
            "credential_env": "ALPHA_KEY",
            "allowed_modes": ["dispatch", "fusion"],
            "allowed_aliases": ["cerberus/mixed", "cerberus/fusion-review"],
        },
        "readonly-client": {
            "credential_env": "BETA_KEY",
            "allowed_modes": ["dispatch"],
            "allowed_aliases": ["cerberus/mixed"],
        },
    }
    raw = raw_config(str(tmp_path / "s.sqlite3"), identities=identities)
    app = build(tmp_path, raw)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            payload = await projection(client)

    config = app.state.lifecycle.active.config
    for entry in payload["aliases"]:
        alias = config.aliases[entry["alias"]]
        assert [row["name"] for row in entry["identities"]] == list(config.identities)
        for row in entry["identities"]:
            expected = authorization_error(
                IdentityContext(name=row["name"], identity=config.identities[row["name"]]),
                entry["alias"],
                alias,
            )
            assert row["denial_reason"] == expected
            assert row["authorized"] is (expected is None)

    fusion_entry = alias_of(payload, "cerberus/fusion-review")
    denied = next(r for r in fusion_entry["identities"] if r["name"] == "readonly-client")
    assert denied["authorized"] is False
    assert denied["denial_reason"] == "alias_not_allowed"


# -- fusion ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fusion_resolves_the_implicit_outer_model(env, tmp_path):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            fusion = alias_of(await projection(client), "cerberus/fusion-review")["fusion"]

    # the config leaves `outer` null; the projection must resolve it to the judge
    raw_outer = app.state.lifecycle.active.config.aliases["cerberus/fusion-review"].fusion.outer
    assert raw_outer is None
    assert fusion["outer"] == {"provider": "alpha", "model": "a-second", "credential_ref": "main"}
    assert fusion["analyst"] == fusion["outer"]
    assert [m["model"] for m in fusion["panel"]] == ["a-free", "a-second"]
    assert [m["order"] for m in fusion["panel"]] == [0, 1]


@pytest.mark.asyncio
async def test_fusion_readiness_matches_its_actual_gates(env, tmp_path, monkeypatch):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            ready = alias_of(await projection(client), "cerberus/fusion-review")["fusion"]["readiness"]
            assert ready == {"backend_present": True, "credential_present": True,
                             "provider_available": True, "available": True}

            # the judge's provider going down is one of fusion's three gates
            app.state.control_plane.set_provider_health("alpha", "down", ttl_seconds=60)
            down = alias_of(await projection(client), "cerberus/fusion-review")["fusion"]["readiness"]

            config = app.state.lifecycle.active.config
            expected = fusion_readiness(
                config, config.aliases["cerberus/fusion-review"],
                backend_names={"openrouter"}, control_plane=app.state.control_plane,
            )

    assert down == expected
    assert down["provider_available"] is False
    assert down["available"] is False


@pytest.mark.asyncio
async def test_fusion_panel_carries_no_failover_or_cooldown_state(env, tmp_path):
    """Fusion never runs its panel through the failover loop, so labelling a
    member eligible/standby/cooled would describe a loop that does not run."""

    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            app.state.cooldowns.apply(
                scope="model", provider="alpha", credential="main", model="a-free",
                reason="quota_429", duration_seconds=3600,
            )
            payload = await projection(client)

    entry = alias_of(payload, "cerberus/fusion-review")
    assert "paths" not in entry
    for member in entry["fusion"]["panel"]:
        assert set(member) == {"order", "provider", "model", "credential_ref"}
    # the very same cooldown does exclude that model on a dispatch alias, so the
    # difference is fusion's contract and not a missing lookup
    assert path_for(alias_of(payload, "cerberus/mixed"), "a-free")["state"] == "excluded"


# -- disclosure and side effects ---------------------------------------------


@pytest.mark.asyncio
async def test_response_carries_credential_references_but_no_secret_or_env_locator(env, tmp_path):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.get("/admin/routes")

    body = response.text
    assert '"credential_ref":"main"' in body
    assert SECRET not in body
    assert "beta-secret-value" not in body
    # not even the name of the variable the secret is read from
    assert "ALPHA_KEY" not in body and "BETA_KEY" not in body
    assert "api_key_env" not in body


@pytest.mark.asyncio
async def test_endpoint_makes_no_upstream_network_call(env, tmp_path):
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return upstream_ok(request)

    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")),
                transport=httpx.MockTransport(record))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await projection(client)

    assert seen == [], f"projection must not call upstream: {seen}"


@pytest.mark.asyncio
async def test_projection_pins_the_active_revision(env, tmp_path):
    app = build(tmp_path, raw_config(str(tmp_path / "s.sqlite3")))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            payload = await projection(client)
            status = (await client.get("/admin/status")).json()

    assert payload["revision"] == status["active"]["version"]
    assert payload["checksum"] == status["active"]["checksum"]
    assert {entry["alias"] for entry in payload["aliases"]} == {
        "cerberus/mixed", "cerberus/fusion-review"
    }
