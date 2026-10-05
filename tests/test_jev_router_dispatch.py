"""jev-router mode: Jev as one model-selection strategy under Cerberus policy.

OpenRouter is mocked at the HTTP boundary (jev_router_transport); no live
traffic. These prove the Cerberus side: Cerberus filters the pool before Jev
sees it and sends it as exact slugs; Jev picks within it; a response from
outside the pool — or without the router's evidence that it stayed inside — is
withheld while its billed usage is still recorded; the caller cannot steer
routing; and a router outage fails only jev-router aliases.
"""

import json

import httpx
import pytest

from cerberus.app import create_app
from cerberus.jev_router import JevRouterError, JevRouterRequest, OpenRouterJevRouterBackend
from cerberus.jev_router.backend import slug_matches
from cerberus.registry import load_config_document
from tests.test_control import ok_upstream, write_config

POOL = ("vendor/model-a", "vendor/model-b")


def jev_raw(*, pool=POOL, allow_paid=False, state_path=None) -> dict:
    raw = {
        "metadata": {"version": "cerberus-2026-10-05.1"},
        "providers": {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "credentials": {"main": {"api_key_env": "OPENROUTER_API_KEY"}},
                "models": {
                    "vendor/model-a": {"cost_tier": "free"},
                    "vendor/model-b": {"cost_tier": "free"},
                    "vendor/model-paid": {"cost_tier": "paid"},
                },
            },
        },
        "identities": {
            "dev": {
                "credential_env": "CB_KEY_DEV",
                "allowed_modes": ["dispatch", "jev-router"],
                "allowed_aliases": ["cerberus/dispatch-dev", "cerberus/auto"],
            },
            "dispatch-only": {
                "credential_env": "CB_KEY_DISPATCH",
                "allowed_modes": ["dispatch"],
                "allowed_aliases": ["cerberus/dispatch-dev"],
            },
        },
        "aliases": {
            "cerberus/dispatch-dev": {
                "mode": "dispatch",
                "candidates": [{"provider": "openrouter", "credential": "main", "model": "vendor/model-a"}],
            },
            "cerberus/auto": {
                "mode": "jev-router",
                "candidates": [{"provider": "openrouter", "credential": "main", "model": m} for m in pool],
                "jev_router": {"timeout_seconds": 30, "allow_paid_pool": allow_paid},
            },
        },
    }
    if state_path is not None:
        raw["state"] = {"path": str(state_path)}
    return raw


class Capture:
    """Records the outbound OpenRouter request so tests can assert the wire shape."""

    def __init__(self, respond):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


def routed(model="vendor/model-a", *, resolved=None, stage=True, list_fallback=None, message=None, **data):
    """A Jev Router completion carrying the documented jev-router pipeline stage."""

    payload = {
        "id": "gen-jev-1",
        "model": model,
        "provider": "SomeProvider",
        "choices": [{"message": message or {"role": "assistant", "content": "routed answer"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.0021},
    }
    if model is None:
        del payload["model"]
    if stage:
        stage_data = {"resolved_models": resolved if resolved is not None else [model], **data}
        if list_fallback is not None:
            stage_data["list_fallback"] = list_fallback
        payload["openrouter_metadata"] = {
            "pipeline": [{"name": "moderation", "data": {}}, {"name": "jev-router", "data": stage_data}]
        }

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return respond


def make_app(monkeypatch, tmp_path, *, raw=None, respond=None, api_key="or-key"):
    if api_key is not None:
        monkeypatch.setenv("OPENROUTER_API_KEY", api_key)
    else:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("CB_KEY_DEV", "cb-dev")
    monkeypatch.setenv("CB_KEY_DISPATCH", "cb-dispatch")
    doc = load_config_document(write_config(tmp_path, "v.yaml", raw or jev_raw()), validate_credentials=False)
    capture = Capture(respond or routed())
    app = create_app(
        doc,
        http_transport=httpx.MockTransport(ok_upstream),
        jev_router_transport=httpx.MockTransport(capture),
    )
    return app, capture


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://localhost")


AUTH = {"authorization": "Bearer cb-dev"}
JEV_REQ = {"model": "cerberus/auto", "messages": [{"role": "user", "content": "hi"}]}
DISPATCH_REQ = {"model": "cerberus/dispatch-dev", "messages": [{"role": "user", "content": "hi"}]}


async def call(app, body=JEV_REQ, headers=AUTH):
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            resp = await client.post("/v1/chat/completions", json=body, headers=headers)
            events = (await client.get("/admin/events")).json()["events"]
    return resp, events


# ---- normal request: wire shape, response contract, telemetry ----


@pytest.mark.asyncio
async def test_request_becomes_one_jev_router_call_over_the_cerberus_pool(monkeypatch, tmp_path):
    app, capture = make_app(monkeypatch, tmp_path)
    resp, _ = await call(app, {**JEV_REQ, "stream": True, "temperature": 0.3})

    assert resp.status_code == 200
    assert len(capture.requests) == 1
    req = capture.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer or-key"
    # the only evidence of whether the list was honoured, so always requested
    assert req.headers["x-openrouter-metadata"] == "enabled"
    assert capture.body == {
        "model": "typesafe/jev-router",
        "plugins": [{"id": "jev-router", "models": ["vendor/model-a", "vendor/model-b"]}],
        "messages": JEV_REQ["messages"],
        "temperature": 0.3,
    }, "exact slugs only, no stream, nothing Cerberus did not construct or allow"

    payload = resp.json()
    assert payload["choices"][0]["message"]["content"] == "routed answer"
    assert "openrouter_metadata" not in payload, "the caller gets the shape it would have had without it"
    assert payload["cerberus"] == {
        "request_id": resp.headers["x-request-id"],
        "alias": "cerberus/auto",
        "mode": "jev-router",
        "identity": "dev",
        "backend": "openrouter",
        "router": "typesafe/jev-router",
        "pool": ["openrouter/vendor/model-a", "openrouter/vendor/model-b"],
        "model": "openrouter/vendor/model-a",
        "decision": {
            "resolved_models": ["vendor/model-a"],
            "list_fallback": None,
            "list_tier_cap": None,
            "max_fallback": None,
        },
        "config_version": "cerberus-2026-10-05.1",
        "config_checksum": app.state.lifecycle.active.checksum,
    }


@pytest.mark.asyncio
async def test_telemetry_records_the_pool_the_decision_and_the_billed_cost(monkeypatch, tmp_path):
    respond = routed("vendor/model-b", resolved=["vendor/model-b", "vendor/model-a"], list_tier_cap="standard")
    app, _ = make_app(monkeypatch, tmp_path, respond=respond)
    resp, events = await call(app)

    event = events[0]
    assert event["request_id"] == resp.headers["x-request-id"]
    assert (event["mode"], event["outcome"], event["http_status"]) == ("jev-router", "success", 200)
    assert event["model"] == "vendor/model-b", "the model Jev chose, not the router alias"
    assert event["cost_tier"] == "free", "known because the served model is a registry member"
    assert event["reported_cost"] == 0.0021
    assert event["token_usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert event["candidates"] == ["openrouter/vendor/model-a", "openrouter/vendor/model-b"]
    assert event["fusion"] is None
    assert event["jev_router"] == {
        "backend": "openrouter",
        "router_model": "typesafe/jev-router",
        "pool": ["vendor/model-a", "vendor/model-b"],
        "returned_model": "vendor/model-b",
        "generation_id": "gen-jev-1",
        "decision": {
            "resolved_models": ["vendor/model-b", "vendor/model-a"],
            "list_fallback": None,
            "list_tier_cap": "standard",
            "max_fallback": None,
        },
        "violation": None,
        "metadata": {"provider": "SomeProvider"},
    }
    assert [a["pool"] for a in event["attempts"]] == ["jev-router"]
    assert event["attempts"][0]["outcome"] == "response"


@pytest.mark.asyncio
async def test_a_dated_revision_of_a_pool_slug_is_inside_the_pool(monkeypatch, tmp_path):
    app, _ = make_app(monkeypatch, tmp_path, respond=routed("vendor/model-a-20260922"))
    resp, events = await call(app)
    assert resp.status_code == 200
    assert events[0]["cost_tier"] == "free"
    assert resp.json()["cerberus"]["model"] == "openrouter/vendor/model-a-20260922"


def test_slug_matching_admits_only_dated_revisions():
    assert slug_matches("vendor/model-a", "vendor/model-a")
    assert slug_matches("vendor/model-a-20260922", "vendor/model-a")
    for outside in ("vendor/model-a-preview", "vendor/model-a-2026", "vendor/model-ab", "vendor/model", "x/vendor/model-a"):
        assert not slug_matches(outside, "vendor/model-a"), outside
    # a registered dated revision matches only itself
    assert not slug_matches("vendor/model-a-20260923", "vendor/model-a-20260922")


# ---- out of policy: withheld, but billed usage recorded ----


@pytest.mark.parametrize(
    ("respond", "violation"),
    [
        # OpenRouter ignored the include list and routed over its whole pool
        (routed("elsewhere/expensive", list_fallback="models_ignored"), "models_ignored"),
        # ... even when the model it happened to pick is one Cerberus allows
        (routed("vendor/model-a", list_fallback="models_ignored"), "models_ignored"),
        (routed("vendor/model-a", list_fallback="something_new"), "list_fallback"),
        (routed("elsewhere/expensive", resolved=["elsewhere/expensive"]), "returned_model_outside_pool"),
        (routed("vendor/model-a", resolved=["vendor/model-a", "elsewhere/expensive"]), "resolved_model_outside_pool"),
        (routed("vendor/model-a-preview"), "returned_model_outside_pool"),
        # no evidence either way is not evidence of staying inside
        (routed("vendor/model-a", stage=False), "decision_unverifiable"),
        (routed(None, resolved=["vendor/model-a"]), "returned_model_unknown"),
    ],
    ids=["ignored-outside", "ignored-inside", "unknown-fallback", "served-outside", "resolved-outside",
         "undated-suffix", "no-stage", "no-model"],
)
@pytest.mark.asyncio
async def test_a_response_outside_the_pool_is_withheld(monkeypatch, tmp_path, respond, violation):
    app, capture = make_app(monkeypatch, tmp_path, respond=respond)
    resp, events = await call(app)

    assert len(capture.requests) == 1
    assert resp.status_code == 502
    body = resp.json()
    assert "choices" not in body, "the answer never reaches the caller"
    assert body["error"]["reason"] == violation
    event = events[0]
    assert (event["outcome"], event["http_status"]) == ("out_of_policy", 502)
    assert event["attempts"][0]["outcome"] == "out_of_policy"
    assert event["jev_router"]["violation"] == violation
    # the call ran and is billed, so its cost is recorded rather than lost
    assert event["token_usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert event["reported_cost"] == 0.0021
    assert event["cost_tier"] is None


@pytest.mark.asyncio
async def test_models_ignored_records_what_actually_ran(monkeypatch, tmp_path):
    app, _ = make_app(monkeypatch, tmp_path, respond=routed("elsewhere/expensive", list_fallback="models_ignored"))
    _, events = await call(app)
    record = events[0]["jev_router"]
    assert record["returned_model"] == "elsewhere/expensive"
    assert record["decision"]["list_fallback"] == "models_ignored"
    assert events[0]["model"] == "elsewhere/expensive"


# ---- Cerberus filters first; Jev only sees what survives ----


@pytest.mark.asyncio
async def test_a_cooled_down_model_never_reaches_jev(monkeypatch, tmp_path):
    app, capture = make_app(monkeypatch, tmp_path)
    app.state.cooldowns.apply(
        scope="model", provider="openrouter", credential="main", model="vendor/model-b",
        reason="quota_429", duration_seconds=600,
    )
    resp, events = await call(app)

    assert resp.status_code == 200
    assert capture.body["plugins"] == [{"id": "jev-router", "models": ["vendor/model-a"]}]
    assert events[0]["jev_router"]["pool"] == ["vendor/model-a"]
    assert events[0]["exclusions"] == [
        {"provider": "openrouter", "credential": "main", "model": "vendor/model-b",
         "reason": "cooldown_quota_429", "scope": "model"}
    ]


@pytest.mark.asyncio
async def test_a_pool_model_jev_was_not_sent_is_outside_the_pool(monkeypatch, tmp_path):
    """Registered is not enough: the pool is what survived the filters."""
    app, _ = make_app(monkeypatch, tmp_path, respond=routed("vendor/model-b"))
    app.state.cooldowns.apply(
        scope="model", provider="openrouter", credential="main", model="vendor/model-b",
        reason="quota_429", duration_seconds=600,
    )
    resp, events = await call(app)
    assert resp.status_code == 502
    assert events[0]["jev_router"]["violation"] == "returned_model_outside_pool"


@pytest.mark.asyncio
async def test_an_empty_pool_fails_closed_without_calling_openrouter(monkeypatch, tmp_path):
    """An empty include list matches nothing, and OpenRouter answers that by
    routing over its whole pool — so an empty pool is never sent."""
    app, capture = make_app(monkeypatch, tmp_path)
    app.state.cooldowns.apply(
        scope="credential", provider="openrouter", credential="main", model=None,
        reason="quota_429", duration_seconds=600,
    )
    resp, events = await call(app)

    assert resp.status_code == 503
    assert capture.requests == []
    assert events[0]["outcome"] == "routing_exhausted"
    assert [e["model"] for e in resp.json()["error"]["exclusions"]] == list(POOL)


@pytest.mark.asyncio
async def test_a_missing_credential_empties_the_pool(monkeypatch, tmp_path):
    app, capture = make_app(monkeypatch, tmp_path, api_key=None)
    resp, events = await call(app)
    assert resp.status_code == 503
    assert capture.requests == []
    assert {e["reason"] for e in events[0]["exclusions"]} == {"missing_credentials"}


# ---- the caller cannot steer routing ----


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("plugins", [{"id": "jev-router", "models": ["elsewhere/*"]}]),
        ("models", ["elsewhere/expensive"]),
        ("route", "fallback"),
        ("provider", {"only": ["SomeProvider"]}),
        ("preset", "@preset/anything"),
        ("transforms", ["middle-out"]),  # unknown fields are refused, not forwarded
        ("reasoning_effort", "high"),  # Jev chooses the effort
        ("reasoning", {"effort": "high"}),
        ("tools", [{"type": "openrouter:fusion", "parameters": {"analysis_models": ["elsewhere/expensive"]}}]),
        ("tools", [{"type": "web_search"}]),
        ("tool_choice", {"type": "openrouter:web_search"}),
    ],
)
@pytest.mark.asyncio
async def test_caller_routing_fields_are_refused_before_any_call(monkeypatch, tmp_path, key, value):
    app, capture = make_app(monkeypatch, tmp_path)
    resp, events = await call(app, {**JEV_REQ, key: value})

    assert resp.status_code == 400
    assert key in resp.json()["error"]["message"]
    assert resp.json()["error"]["reason"] == "caller_field_not_allowed"
    assert capture.requests == [], "nothing may reach OpenRouter"
    assert (events[0]["outcome"], events[0]["http_status"]) == ("invalid_request", 400)
    assert events[0]["attempts"] == []


@pytest.mark.asyncio
async def test_function_tools_pass_through_and_a_tool_call_answer_is_accepted(monkeypatch, tmp_path):
    tool_call = {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}
    app, capture = make_app(monkeypatch, tmp_path, respond=routed(message=tool_call))
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    resp, _ = await call(app, {**JEV_REQ, "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False})

    assert resp.status_code == 200
    assert capture.body["tools"] == tools
    assert capture.body["tool_choice"] == "auto"
    assert resp.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "lookup"


# ---- policy boundary ----


@pytest.mark.asyncio
async def test_authorization_gates_jev_router_before_any_call(monkeypatch, tmp_path):
    app, capture = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            anonymous = await client.post("/v1/chat/completions", json=JEV_REQ)
            denied = await client.post(
                "/v1/chat/completions", json=JEV_REQ, headers={"authorization": "Bearer cb-dispatch"}
            )
    assert anonymous.status_code == 401
    assert denied.status_code == 403
    assert capture.requests == []


@pytest.mark.asyncio
async def test_a_router_outage_fails_only_jev_router(monkeypatch, tmp_path):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    app, _ = make_app(monkeypatch, tmp_path, respond=unreachable)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            jev = await client.post("/v1/chat/completions", json=JEV_REQ, headers=AUTH)
            dispatch = await client.post("/v1/chat/completions", json=DISPATCH_REQ, headers=AUTH)
            events = (await client.get("/admin/events")).json()["events"]

    assert jev.status_code == 503
    assert dispatch.status_code == 200
    event = next(e for e in events if e["mode"] == "jev-router")
    assert event["outcome"] == "jev_router_unavailable"
    assert event["attempts"][0]["outcome"] == "transport_error"


@pytest.mark.parametrize(
    ("respond", "status", "outcome", "attempt_outcome"),
    [
        (lambda r: httpx.Response(404, json={"error": "widen allowed_models"}), 502, "upstream_error", "invalid_response"),
        (lambda r: httpx.Response(429, json={"error": "rate"}), 502, "upstream_error", "retryable_status"),
        (lambda r: httpx.Response(503, text="down"), 503, "jev_router_unavailable", "retryable_status"),
        (lambda r: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
         502, "upstream_error", "invalid_response"),
        (lambda r: httpx.Response(200, json={"id": "g", "choices": [{"message": {"role": "assistant"}}]}),
         502, "upstream_error", "invalid_response"),
    ],
    ids=["http-404", "http-429", "http-503", "invalid-json", "no-content"],
)
@pytest.mark.asyncio
async def test_bad_router_responses_fail_closed(monkeypatch, tmp_path, respond, status, outcome, attempt_outcome):
    app, _ = make_app(monkeypatch, tmp_path, respond=respond)
    resp, events = await call(app)
    assert resp.status_code == status
    assert "choices" not in resp.json()
    assert events[0]["outcome"] == outcome
    assert events[0]["attempts"][0]["outcome"] == attempt_outcome


# ---- admin projection and control-plane records ----


@pytest.mark.asyncio
async def test_routes_project_the_pool_jev_would_be_sent(monkeypatch, tmp_path):
    app, _ = make_app(monkeypatch, tmp_path)
    app.state.cooldowns.apply(
        scope="model", provider="openrouter", credential="main", model="vendor/model-b",
        reason="quota_429", duration_seconds=600,
    )
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            projection = (await client.get("/admin/routes")).json()

    entry = next(a for a in projection["aliases"] if a["alias"] == "cerberus/auto")
    assert entry["mode"] == "jev-router"
    assert "paths" not in entry and "fusion" not in entry
    jev = entry["jev_router"]
    assert (jev["backend"], jev["router_model"], jev["allow_paid_pool"]) == ("openrouter", "typesafe/jev-router", False)
    assert [(m["model"], m["state"]) for m in jev["pool"]] == [
        ("vendor/model-a", "eligible"),
        ("vendor/model-b", "excluded"),
    ], "a pool has no preference order, so never a standby"
    assert jev["pool"][1]["exclusion"]["reason"] == "cooldown_quota_429"
    assert jev["readiness"] == {"backend_present": True, "pool_available": 1, "pool_size": 2, "available": True}
    serialized = json.dumps(projection)
    assert "OPENROUTER_API_KEY" not in serialized and "or-key" not in serialized


@pytest.mark.asyncio
async def test_revision_materializes_the_jev_router_pool(monkeypatch, tmp_path):
    import sqlite3

    state = tmp_path / "state.sqlite3"
    app, _ = make_app(monkeypatch, tmp_path, raw=jev_raw(state_path=state))
    async with app.router.lifespan_context(app):
        pass
    connection = sqlite3.connect(state)
    try:
        binding = connection.execute(
            "SELECT binding_type FROM alias_bindings WHERE alias='cerberus/auto'"
        ).fetchone()
        members = connection.execute(
            "SELECT role, ordinal, model FROM route_members WHERE alias='cerberus/auto' ORDER BY ordinal"
        ).fetchall()
    finally:
        connection.close()
    assert binding == ("jev-router",)
    assert members == [("pool", 0, "vendor/model-a"), ("pool", 1, "vendor/model-b")]


# ---- backend unit: wire shape independent of the app ----


def test_backend_builds_the_documented_body_and_owns_routing_fields():
    request = JevRouterRequest(
        body={"messages": [], "model": "cerberus/auto", "stream": True, "stream_options": {"include_usage": True},
              "plugins": [{"id": "x"}], "seed": 7},
        pool_models=["vendor/model-a"],
        base_url="https://openrouter.ai/api/v1/",
        api_key="k",
        timeout_seconds=5,
    )
    assert OpenRouterJevRouterBackend.build_body(request) == {
        "messages": [],
        "seed": 7,
        "model": "typesafe/jev-router",
        "plugins": [{"id": "jev-router", "models": ["vendor/model-a"]}],
    }


@pytest.mark.asyncio
async def test_backend_refuses_an_empty_pool_and_normalizes_errors():
    backend = OpenRouterJevRouterBackend(httpx.MockTransport(routed()))
    empty = JevRouterRequest(body={"messages": []}, pool_models=[], base_url="https://openrouter.ai/api/v1",
                             api_key="k", timeout_seconds=5)
    with pytest.raises(ValueError, match="non-empty pool"):
        await backend.execute(empty)
    await backend.aclose()

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    backend = OpenRouterJevRouterBackend(httpx.MockTransport(slow))
    request = JevRouterRequest(body={"messages": []}, pool_models=["vendor/model-a"],
                               base_url="https://openrouter.ai/api/v1", api_key="k", timeout_seconds=5)
    with pytest.raises(JevRouterError) as info:
        await backend.execute(request)
    assert (info.value.http_status, info.value.outcome) == (503, "jev_router_unavailable")
    await backend.aclose()
