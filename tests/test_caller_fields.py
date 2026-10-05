"""Caller request bodies are restricted before they reach a provider.

Provider extensions (OpenRouter's `models` fallback list, `provider`, `route`,
`plugins`, server tools like `openrouter:fusion`) can select or add models the
alias never listed. Forwarding them verbatim let a caller on a free-only alias
spend paid quota under Cerberus's credential.
"""

import json

import httpx
import pytest

from cerberus.app import create_app
from cerberus.caller_fields import caller_body, non_function_tools
from cerberus.fusion.backend import FusionRequest, OpenRouterFusionBackend
from cerberus.registry import load_config_document
from tests.test_app import call, make_config
from tests.test_control import ok_upstream, write_config
from tests.test_fusion_dispatch import AUTH, DISPATCH_REQ, Capture, client_for, fusion_raw

MESSAGES = [{"role": "user", "content": "hi"}]


def test_caller_body_drops_provider_extensions():
    body = {
        "messages": MESSAGES,
        "temperature": 0.1,
        "models": ["paid/model"],
        "provider": {"order": ["x"]},
        "route": "fallback",
        "plugins": [{"id": "web"}],
        "service_tier": "priority",
    }
    assert caller_body(body) == {"messages": MESSAGES, "temperature": 0.1}


def test_non_function_tools_are_named():
    assert non_function_tools({"tools": [{"type": "function", "function": {"name": "f"}}]}) == []
    assert non_function_tools({"tools": [{"type": "openrouter:fusion"}]}) == ["openrouter:fusion"]
    assert non_function_tools({"tool_choice": {"type": "web_search"}}) == ["web_search"]
    assert non_function_tools({"tools": ["bogus"]}) == ["None"]
    assert non_function_tools({"tool_choice": "auto"}) == []


@pytest.mark.asyncio
async def test_dispatch_forwards_only_standard_fields(monkeypatch):
    seen = []

    async def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "cerberus/main", "messages": MESSAGES, "models": ["paid/model"], "plugins": [{"id": "web"}]},
    )
    assert response.status_code == 200, response.text
    assert seen == [{"model": "alpha-free", "messages": MESSAGES}]


@pytest.mark.asyncio
async def test_dispatch_refuses_server_tools(monkeypatch):
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={})

    app = create_app(make_config(monkeypatch), http_transport=httpx.MockTransport(upstream))
    response = await call(
        app,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "cerberus/main",
            "messages": MESSAGES,
            "tools": [{"type": "openrouter:fusion", "parameters": {"analysis_models": ["paid/model"]}}],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["reason"] == "unsupported_tool"
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"type": "openrouter:fusion", "parameters": {"analysis_models": ["paid/model"]}}]},
        {"tool_choice": {"type": "openrouter:fusion"}},
    ],
    ids=["tools", "tool_choice"],
)
async def test_server_tool_refusal_is_a_routing_event(monkeypatch, tmp_path, extra):
    """The refusal is recorded like every other routing decision, under the
    caller's identity, and the response names the event's request id."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("CB_KEY_DEV", "cb-dev")
    monkeypatch.setenv("CB_KEY_DISPATCH", "cb-dispatch")
    doc = load_config_document(write_config(tmp_path, "v.yaml", fusion_raw()), validate_credentials=False)
    upstream = Capture(ok_upstream)
    app = create_app(doc, http_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.post("/v1/chat/completions", json={**DISPATCH_REQ, **extra}, headers=AUTH)
            events = (await client.get("/admin/events")).json()["events"]

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["reason"] == "unsupported_tool"
    assert upstream.requests == []
    assert len(events) == 1
    event = events[0]
    assert (event["outcome"], event["http_status"]) == ("invalid_request", 400)
    assert (event["alias"], event["mode"], event["identity"]) == ("cerberus/dispatch-dev", "dispatch", "dev")
    assert event["request_id"] == error["request_id"]
    assert event["attempt_count"] == 0 and event["provider"] is None and event["model"] is None
    assert event["config_version"] == doc.version and event["config_checksum"] == doc.checksum


def test_fusion_body_drops_provider_extensions():
    request = FusionRequest(
        body={"messages": MESSAGES, "models": ["paid/model"], "provider": {"order": ["x"]}, "route": "fallback"},
        panel_models=["a"],
        analyst_model="c",
        outer_model="d",
        base_url="https://openrouter.ai/api/v1",
        api_key="k",
        timeout_seconds=5,
    )
    body = OpenRouterFusionBackend.build_body(request)
    assert set(body) == {"messages", "model", "tools", "tool_choice"}
