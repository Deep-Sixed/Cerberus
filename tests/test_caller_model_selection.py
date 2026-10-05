"""A request body cannot choose models upstream on a dispatch-path alias.

``build_upstream_request`` overwrites only ``model``, so any other key reaches
the provider as sent. OpenRouter reads several of them as model selection
(``models``/``route`` fallback lists, ``plugins``, ``preset``, ``openrouter:*``
server tools), which would let a caller run models absent from the pinned
revision on the operator's credential. The upstream is mocked at the HTTP
boundary; these prove such a body is refused before it, while tool calling and
keys that leave the model alone are forwarded untouched.
"""

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import load_config_document
from cerberus.router.engine import caller_model_selection
from tests.test_control import ok_upstream, write_config
from tests.test_fusion_dispatch import AUTH, Capture, client_for, fusion_raw

FUSION_TOOL = {
    "type": "openrouter:fusion",
    "parameters": {"analysis_models": ["openai/gpt-5", "anthropic/claude-opus-4"], "model": "openai/gpt-5"},
}
FUNCTION_TOOL = {
    "type": "function",
    "function": {"name": "lookup", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}},
}
ALIASES = ("cerberus/dispatch-dev", "cerberus/free-dev")


def raw_config() -> dict:
    """The fusion-dev slice (an OpenRouter dispatch candidate) plus a free alias."""
    raw = fusion_raw()
    raw["aliases"]["cerberus/free-dev"] = {
        "mode": "free",
        "candidates": [{"provider": "openrouter", "credential": "main", "model": "free-b"}],
    }
    raw["identities"]["dev"]["allowed_modes"].append("free")
    raw["identities"]["dev"]["allowed_aliases"].append("cerberus/free-dev")
    return raw


def make_app(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("CB_KEY_DEV", "cb-dev")
    monkeypatch.setenv("CB_KEY_DISPATCH", "cb-dispatch")
    doc = load_config_document(write_config(tmp_path, "v.yaml", raw_config()), validate_credentials=False)
    upstream = Capture(ok_upstream)
    return create_app(doc, http_transport=httpx.MockTransport(upstream)), upstream


async def call(app, body):
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            resp = await client.post("/v1/chat/completions", json=body, headers=AUTH)
            events = (await client.get("/admin/events")).json()["events"]
    return resp, events


def request(alias: str, **extra) -> dict:
    return {"model": alias, "messages": [{"role": "user", "content": "hi"}], **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize(
    ("key", "extra"),
    [
        ("models", {"models": ["openai/gpt-5", "anthropic/claude-opus-4"]}),
        ("route", {"route": "fallback"}),
        ("plugins", {"plugins": [{"id": "jev-router"}]}),
        ("preset", {"preset": "operator-preset"}),
        ("tools", {"tools": [FUNCTION_TOOL, FUSION_TOOL]}),
        ("tool_choice", {"tool_choice": {"type": "openrouter:fusion"}}),
        ("models", {"models": ["openai/gpt-5"], "stream": True}),
    ],
    ids=["models", "route", "plugins", "preset", "openrouter-fusion-tool", "openrouter-tool-choice", "streaming"],
)
async def test_model_selection_keys_are_rejected_before_any_upstream_call(monkeypatch, tmp_path, alias, key, extra):
    app, upstream = make_app(monkeypatch, tmp_path)
    resp, events = await call(app, request(alias, **extra))

    assert upstream.requests == [], "a body that selects models must never reach the provider"
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert key in error["message"] and error["request_id"]
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "upstream_error" and event["http_status"] == 400
    assert event["alias"] == alias and event["identity"] == "dev"
    assert event["request_id"] == error["request_id"]
    assert event["attempt_count"] == 0 and event["provider"] is None and event["model"] is None
    assert event["streaming"] is bool(extra.get("stream"))


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ALIASES)
async def test_function_tools_and_non_routing_keys_are_forwarded_unchanged(monkeypatch, tmp_path, alias):
    app, upstream = make_app(monkeypatch, tmp_path)
    tool_choice = {"type": "function", "function": {"name": "lookup"}}
    # provider preferences narrow which host serves the pinned model; they never pick another one
    provider = {"data_collection": "deny", "allow_fallbacks": False}
    resp, events = await call(
        app, request(alias, tools=[FUNCTION_TOOL], tool_choice=tool_choice, provider=provider, temperature=0.2)
    )

    assert resp.status_code == 200
    assert len(upstream.requests) == 1
    sent = upstream.body
    assert sent["model"] == ("free-a" if alias == "cerberus/dispatch-dev" else "free-b")
    assert sent["tools"] == [FUNCTION_TOOL]
    assert sent["tool_choice"] == tool_choice
    assert sent["provider"] == provider and sent["temperature"] == 0.2
    assert events[0]["outcome"] == "success"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"messages": []}, []),
        ({"tools": [FUNCTION_TOOL], "tool_choice": "auto"}, []),
        ({"tool_choice": "required", "provider": {"order": ["x"]}}, []),
        ({"tools": None, "tool_choice": None}, []),
        ({"tools": [{"type": "OpenRouter:Web_Search"}]}, ["tools"]),
        ({"tools": FUSION_TOOL}, ["tools"]),
        ({"tool_choice": "openrouter:fusion"}, ["tool_choice"]),
        ({"models": [], "route": None, "plugins": [], "preset": ""}, ["models", "route", "plugins", "preset"]),
    ],
)
def test_caller_model_selection_classifies_bodies(body, expected):
    assert caller_model_selection(body) == expected
