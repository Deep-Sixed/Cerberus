"""Fusion mode: Cerberus policy in front of OpenRouter's managed Fusion Router.

OpenRouter is mocked at the HTTP boundary (fusion_transport); no live traffic.
These prove the Cerberus side: a fusion alias becomes ONE ``openrouter/fusion``
call whose panel/analyst come from Cerberus policy with the deliberation forced;
authorization and alias policy still gate the route; every backend failure fails
closed for fusion aliases only; the response and telemetry contracts hold.
"""

import json

import httpx
import pytest

from cerberus.app import create_app
from cerberus.fusion import FusionError, FusionRequest, OpenRouterFusionBackend
from cerberus.registry import load_config_document
from tests.test_control import ok_upstream, write_config


def fusion_raw(*, panel=("free-a", "free-b"), judge="free-a") -> dict:
    return {
        "metadata": {"version": "cerberus-2026-09-14.1"},
        "providers": {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "credentials": {"main": {"api_key_env": "OPENROUTER_API_KEY"}},
                "models": {
                    "free-a": {"cost_tier": "free"},
                    "free-b": {"cost_tier": "free"},
                    "free-c": {"cost_tier": "free"},
                },
            },
        },
        "identities": {
            "dev": {
                "credential_env": "CB_KEY_DEV",
                "allowed_modes": ["dispatch", "fusion"],
                "allowed_aliases": ["cerberus/dispatch-dev", "cerberus/fusion-dev"],
                "default_alias": "cerberus/dispatch-dev",
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
                "candidates": [{"provider": "openrouter", "credential": "main", "model": "free-a"}],
            },
            "cerberus/fusion-dev": {
                "mode": "fusion",
                "candidates": [{"provider": "openrouter", "credential": "main", "model": m} for m in panel],
                "fusion": {
                    "backend": "openrouter",
                    "max_panel_members": 5,
                    "timeout_seconds": 30,
                    "allow_paid_panel": False,
                    "judge": {"provider": "openrouter", "credential": "main", "model": judge},
                },
            },
        },
    }


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


def openrouter_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "gen-abc123",
            "model": "free-a",
            "provider": "SomeProvider",
            "choices": [{"message": {"role": "assistant", "content": "synthesized answer"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def make_app(monkeypatch, tmp_path, *, raw=None, respond=openrouter_ok, api_key="or-key"):
    if api_key is not None:
        monkeypatch.setenv("OPENROUTER_API_KEY", api_key)
    else:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("CB_KEY_DEV", "cb-dev")
    monkeypatch.setenv("CB_KEY_DISPATCH", "cb-dispatch")
    doc = load_config_document(write_config(tmp_path, "v.yaml", raw or fusion_raw()), validate_credentials=False)
    capture = Capture(respond)
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream), fusion_transport=httpx.MockTransport(capture))
    return app, capture


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://t")


AUTH = {"authorization": "Bearer cb-dev"}
FUSION_REQ = {"model": "cerberus/fusion-dev", "messages": [{"role": "user", "content": "hi"}]}
DISPATCH_REQ = {"model": "cerberus/dispatch-dev", "messages": [{"role": "user", "content": "hi"}]}


async def call(app, body=FUSION_REQ, headers=AUTH):
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            resp = await client.post("/v1/chat/completions", json=body, headers=headers)
            events = (await client.get("/admin/events")).json()["events"]
    return resp, events


# ---- normal request: wire shape, response contract, telemetry ----


@pytest.mark.asyncio
async def test_fusion_request_becomes_one_forced_openrouter_fusion_call(monkeypatch, tmp_path):
    app, capture = make_app(monkeypatch, tmp_path)
    resp, events = await call(app)

    assert resp.status_code == 200
    # exactly one upstream call, to OpenRouter chat completions, under the judge credential
    assert len(capture.requests) == 1
    req = capture.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer or-key"
    body = capture.body
    assert body["model"] == "openrouter/fusion"
    assert body["plugins"] == [{"id": "fusion", "analysis_models": ["free-a", "free-b"], "model": "free-a"}]
    assert body["tool_choice"] == "required", "Cerberus policy chose fusion; the outer model may not skip it"
    assert body["messages"] == FUSION_REQ["messages"]
    assert "stream" not in body

    # OpenAI-compatible contract preserved, Cerberus envelope attached
    payload = resp.json()
    assert payload["choices"][0]["message"]["content"] == "synthesized answer"
    assert payload["cerberus"] == {
        "request_id": resp.headers["x-request-id"],
        "alias": "cerberus/fusion-dev",
        "mode": "fusion",
        "identity": "dev",
        "backend": "openrouter",
        "panel": ["openrouter/free-a", "openrouter/free-b"],
        "judge": "openrouter/free-a",
        "config_version": "cerberus-2026-09-14.1",
    }


@pytest.mark.asyncio
async def test_fusion_telemetry_is_accurate_and_never_per_seat(monkeypatch, tmp_path):
    app, _ = make_app(monkeypatch, tmp_path)
    resp, events = await call(app)

    event = events[0]
    assert event["request_id"] == resp.headers["x-request-id"]
    assert event["mode"] == "fusion" and event["outcome"] == "success" and event["http_status"] == 200
    assert event["alias"] == "cerberus/fusion-dev" and event["identity"] == "dev"
    assert event["candidates"] == ["openrouter/free-a", "openrouter/free-b"]
    assert event["token_usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert event["model"] == "free-a"  # the model OpenRouter reported, not the alias
    assert event["fusion"] == {
        "backend": "openrouter",
        "panel": ["openrouter/free-a", "openrouter/free-b"],
        "analyst": "openrouter/free-a",
        "returned_model": "free-a",
        "generation_id": "gen-abc123",
        "metadata": {"router": "openrouter/fusion", "provider": "SomeProvider"},
    }
    # OpenRouter exposes no per-seat outcomes, so Cerberus reports the ONE call it made
    assert [a["pool"] for a in event["attempts"]] == ["fusion"]
    assert event["attempts"][0]["outcome"] == "response" and event["attempts"][0]["http_status"] == 200
    assert event["latency_ms"] >= event["attempts"][0]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_custom_panel_and_analyst_come_from_cerberus_policy(monkeypatch, tmp_path):
    raw = fusion_raw(panel=("free-c", "free-b", "free-a"), judge="free-c")
    app, capture = make_app(monkeypatch, tmp_path, raw=raw)
    resp, events = await call(app)

    assert resp.status_code == 200
    assert capture.body["plugins"][0]["analysis_models"] == ["free-c", "free-b", "free-a"]
    assert capture.body["plugins"][0]["model"] == "free-c"
    assert resp.json()["cerberus"]["judge"] == "openrouter/free-c"
    assert events[0]["fusion"]["analyst"] == "openrouter/free-c"


@pytest.mark.asyncio
async def test_caller_cannot_override_panel_or_tool_surface(monkeypatch, tmp_path):
    """Cerberus, not the caller, decides the panel and forces the deliberation."""
    app, capture = make_app(monkeypatch, tmp_path)
    # model/stream are Cerberus-owned and silently replaced ...
    resp, _ = await call(app, {**FUSION_REQ, "stream": True})
    assert resp.status_code == 200
    assert capture.body["model"] == "openrouter/fusion" and "stream" not in capture.body
    # ... but tools/tool_choice/plugins cannot be honored through a fusion alias: reject, never forward
    for key, value in (("plugins", [{"id": "fusion", "analysis_models": ["free-c"]}]), ("tool_choice", "none"),
                       ("tools", [{"type": "function", "function": {"name": "x"}}])):
        app, capture = make_app(monkeypatch, tmp_path)
        resp, events = await call(app, {**FUSION_REQ, key: value})
        assert resp.status_code == 400, key
        assert key in resp.json()["error"]["message"]
        assert capture.requests == [], "nothing may reach OpenRouter"
        assert events[0]["outcome"] == "upstream_error" and events[0]["http_status"] == 400


# ---- policy boundary ----


@pytest.mark.asyncio
async def test_auth_and_alias_policy_gate_fusion_before_any_backend_call(monkeypatch, tmp_path):
    app, capture = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            anonymous = await client.post("/v1/chat/completions", json=FUSION_REQ)
            wrong_key = await client.post("/v1/chat/completions", json=FUSION_REQ, headers={"authorization": "Bearer nope"})
            # a valid identity whose allowed_modes/aliases exclude fusion
            denied = await client.post("/v1/chat/completions", json=FUSION_REQ, headers={"authorization": "Bearer cb-dispatch"})
            events = (await client.get("/admin/events")).json()["events"]

    assert anonymous.status_code == 401
    assert wrong_key.status_code == 401
    assert denied.status_code == 403
    assert capture.requests == [], "OpenRouter must never be called for a request Cerberus denied"
    assert events[0]["outcome"] == "unauthorized" and events[0]["identity"] == "dispatch-only"


@pytest.mark.asyncio
async def test_non_fusion_routes_are_unchanged(monkeypatch, tmp_path):
    app, capture = make_app(monkeypatch, tmp_path)
    resp, events = await call(app, DISPATCH_REQ)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"
    assert capture.requests == []
    assert events[0]["mode"] == "dispatch" and events[0]["fusion"] is None


# ---- fail closed ----


@pytest.mark.asyncio
async def test_missing_openrouter_credential_fails_only_fusion(monkeypatch, tmp_path):
    app, capture = make_app(monkeypatch, tmp_path, api_key=None)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            status = (await client.get("/admin/status")).json()
            fusion = await client.post("/v1/chat/completions", json=FUSION_REQ, headers=AUTH)
            events = (await client.get("/admin/events")).json()["events"]

    assert status["fusion"] == {"state": "not_configured", "backends": ["openrouter"], "aliases": ["cerberus/fusion-dev"]}
    assert fusion.status_code == 503
    assert "not configured" in fusion.json()["error"]["message"].lower()
    assert capture.requests == []
    assert events[0]["outcome"] == "fusion_unavailable"


@pytest.mark.asyncio
async def test_configured_status_is_truthful(monkeypatch, tmp_path):
    app, _ = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            status = (await client.get("/admin/status")).json()
    assert status["fusion"]["state"] == "configured"


@pytest.mark.asyncio
async def test_openrouter_timeout_fails_fusion_and_leaves_dispatch_working(monkeypatch, tmp_path):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    app, _ = make_app(monkeypatch, tmp_path, respond=timeout)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            fusion = await client.post("/v1/chat/completions", json=FUSION_REQ, headers=AUTH)
            dispatch = await client.post("/v1/chat/completions", json=DISPATCH_REQ, headers=AUTH)
            events = (await client.get("/admin/events")).json()["events"]

    assert fusion.status_code == 503
    assert dispatch.status_code == 200  # degradation is isolated to fusion
    event = next(e for e in events if e["mode"] == "fusion")
    assert event["outcome"] == "fusion_unavailable"
    assert event["attempts"][0]["outcome"] == "transport_error"
    assert event["fusion"]["returned_model"] is None and event["fusion"]["generation_id"] is None


@pytest.mark.parametrize(
    ("respond", "status", "outcome", "attempt_outcome"),
    [
        (lambda r: httpx.Response(429, json={"error": "rate"}), 502, "upstream_error", "retryable_status"),
        (lambda r: httpx.Response(400, json={"error": "bad"}), 502, "upstream_error", "invalid_response"),
        (lambda r: httpx.Response(503, text="down"), 503, "fusion_unavailable", "retryable_status"),
        (lambda r: httpx.Response(200, content=b"<html>not json", headers={"content-type": "text/html"}),
         502, "upstream_error", "invalid_response"),
        (lambda r: httpx.Response(200, json={"id": "gen-1", "choices": []}), 502, "upstream_error", "invalid_response"),
        (lambda r: httpx.Response(200, json={"id": "gen-1", "choices": [{"message": {"role": "assistant"}}]}),
         502, "upstream_error", "invalid_response"),
        (lambda r: httpx.Response(200, json=["not", "an", "object"]), 502, "upstream_error", "invalid_response"),
    ],
    ids=["http-429", "http-400", "http-503", "invalid-json", "no-choices", "missing-content", "non-object"],
)
@pytest.mark.asyncio
async def test_bad_openrouter_responses_fail_closed(monkeypatch, tmp_path, respond, status, outcome, attempt_outcome):
    app, _ = make_app(monkeypatch, tmp_path, respond=respond)
    resp, events = await call(app)

    assert resp.status_code == status
    error = resp.json()["error"]
    assert error["request_id"] == events[0]["request_id"]
    assert "choices" not in resp.json(), "a malformed completion is never passed through"
    assert events[0]["outcome"] == outcome
    assert events[0]["attempts"][0]["outcome"] == attempt_outcome
    assert events[0]["token_usage"] is None


# ---- backend unit: wire shape independent of the app ----


def test_openrouter_backend_builds_documented_fusion_body():
    request = FusionRequest(
        body={"messages": [{"role": "user", "content": "q"}], "temperature": 0.2, "model": "ignored",
              "plugins": [{"id": "web"}], "tool_choice": "auto", "stream": True},
        panel_models=["a", "b"],
        analyst_model="c",
        base_url="https://openrouter.ai/api/v1/",
        api_key="k",
        timeout_seconds=5,
    )
    body = OpenRouterFusionBackend.build_body(request)
    assert body == {
        "messages": [{"role": "user", "content": "q"}],
        "temperature": 0.2,
        "model": "openrouter/fusion",
        "plugins": [{"id": "fusion", "analysis_models": ["a", "b"], "model": "c"}],
        "tool_choice": "required",
    }


@pytest.mark.asyncio
async def test_openrouter_backend_normalizes_result_and_errors():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer k"
        return openrouter_ok(request)

    backend = OpenRouterFusionBackend(httpx.MockTransport(respond))
    request = FusionRequest(body={"messages": []}, panel_models=["a"], analyst_model="c",
                            base_url="https://openrouter.ai/api/v1", api_key="k", timeout_seconds=5)
    result = await backend.execute(request)
    assert (result.returned_model, result.generation_id, result.http_status) == ("free-a", "gen-abc123", 200)
    assert result.metadata == {"router": "openrouter/fusion", "provider": "SomeProvider"}
    await backend.aclose()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    backend = OpenRouterFusionBackend(httpx.MockTransport(down))
    with pytest.raises(FusionError) as info:
        await backend.execute(request)
    assert (info.value.http_status, info.value.outcome) == (503, "fusion_unavailable")
    await backend.aclose()
