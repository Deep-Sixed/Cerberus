"""jev mode: Cerberus asks Jev which model, then runs it through its own loop.

Both the Decisions API (jev_transport) and every generation upstream
(http_transport) are mocked at the HTTP boundary; no live traffic. These prove:
the decision request's wire shape and what it may read; that Jev's choice only
reorders models policy already admitted, across providers; that any failure to
get a usable answer runs the request in configured order; and that no request
text reaches telemetry.
"""

import json

import httpx
import pytest

from cerberus.app import create_app
from cerberus.jev import DecisionRequest, OpenRouterJevDecider
from cerberus.jev.backend import _route_answer
from cerberus.registry import load_config_document
from tests.test_control import write_config

LOCAL = ("edgebox", "gemma-4-12b")
MID = ("openrouter", "vendor/mid")
FRONTIER = ("openrouter", "anthropic/claude-sonnet")
SECRET_TURN = "first-turn-private-detail"
LATEST = "latest-question-text"


def jev_raw(*, jev=None, candidates=(LOCAL, MID, FRONTIER), state_path=None) -> dict:
    raw = {
        "metadata": {"version": "cerberus-2026-10-05.2"},
        "providers": {
            "edgebox": {
                "base_url": "http://edgebox.test/v1",
                "credentials": {"none": {"api_key_env": "EDGE_KEY"}},
                "models": {
                    "gemma-4-12b": {"cost_tier": "free", "strength": "standard", "context_window": 32768,
                                    "capabilities": ["chat", "coding"], "description": "Local 12B, fast."},
                },
            },
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "credentials": {
                    "main": {"api_key_env": "OPENROUTER_API_KEY"},
                    "decisions": {"api_key_env": "JEV_DECISIONS_KEY"},
                },
                "models": {
                    "vendor/mid": {"cost_tier": "free", "strength": "strong"},
                    "anthropic/claude-sonnet": {"cost_tier": "paid", "strength": "frontier"},
                },
            },
        },
        "identities": {
            "dev": {
                "credential_env": "CB_KEY_DEV",
                "allowed_modes": ["jev"],
                "allowed_aliases": ["cerberus/smart"],
            },
        },
        "aliases": {
            "cerberus/smart": {
                "mode": "jev",
                "candidates": [
                    {"provider": p, "credential": "none" if p == "edgebox" else "main", "model": m}
                    for p, m in candidates
                ],
                "jev": {
                    "decider": {"provider": "openrouter", "credential": "decisions"},
                    "allow_paid_pool": True,
                    **(jev or {}),
                },
            },
        },
    }
    if state_path is not None:
        raw["state"] = {"path": str(state_path)}
    return raw


class Capture:
    def __init__(self, respond):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)

    def models(self) -> list[str]:
        return [json.loads(r.content)["model"] for r in self.requests]


def answer(payload, status=200):
    def respond(_request):
        return httpx.Response(status, json=payload)

    return respond


def chooses(option, probability=0.91):
    return answer({
        "answers": {"route": {"type": "choice", "choice": option, "probabilities": {option: probability}}},
        "usage": {"prompt_tokens": 120, "completion_tokens": 1, "total_tokens": 121, "cost": 0.00004},
    })


def generation_ok(request: httpx.Request) -> httpx.Response:
    model = json.loads(request.content)["model"]
    return httpx.Response(200, json={
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": f"answer from {model}"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    })


def make_app(monkeypatch, tmp_path, *, raw=None, decide=None, generate=generation_ok, decider_key="jev-key"):
    monkeypatch.setenv("EDGE_KEY", "local-no-auth")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("CB_KEY_DEV", "cb-dev")
    if decider_key is None:
        monkeypatch.delenv("JEV_DECISIONS_KEY", raising=False)
    else:
        monkeypatch.setenv("JEV_DECISIONS_KEY", decider_key)
    doc = load_config_document(write_config(tmp_path, "v.yaml", raw or jev_raw()), validate_credentials=False)
    decisions = Capture(decide or chooses("m2"))
    upstream = Capture(generate)
    app = create_app(doc, http_transport=httpx.MockTransport(upstream), jev_transport=httpx.MockTransport(decisions))
    return app, decisions, upstream


AUTH = {"authorization": "Bearer cb-dev"}
CONVERSATION = [
    {"role": "user", "content": SECRET_TURN},
    {"role": "assistant", "content": "an earlier answer"},
    {"role": "user", "content": LATEST},
]
REQ = {"model": "cerberus/smart", "messages": CONVERSATION}


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://t")


async def call(app, body=REQ):
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            resp = await client.post("/v1/chat/completions", json=body, headers=AUTH)
            if body.get("stream"):
                await resp.aread()
            events = (await client.get("/admin/events")).json()["events"]
    return resp, events


# ---- the decision request ----


@pytest.mark.asyncio
async def test_the_decision_is_one_decisions_api_call_with_the_pinned_model(monkeypatch, tmp_path):
    app, decisions, _ = make_app(monkeypatch, tmp_path)
    await call(app)

    assert len(decisions.requests) == 1
    req = decisions.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/alpha/decisions"
    assert req.headers["authorization"] == "Bearer jev-key", "the decider's own credential"
    body = decisions.body
    assert body["model"] == "typesafe/jev-1.13"
    assert body["questions"] == [{
        "id": "route",
        "type": "choice",
        "question": "Which candidate model is the least costly one that is still strong enough "
                    "to handle this request well?",
        "options": ["m1", "m2", "m3"],
    }]
    assert set(body) == {"model", "state", "questions"}


@pytest.mark.asyncio
async def test_the_decision_reads_only_the_latest_user_message_by_default(monkeypatch, tmp_path):
    app, decisions, _ = make_app(monkeypatch, tmp_path)
    await call(app)
    state = decisions.body["state"]

    assert LATEST in state
    assert SECRET_TURN not in state and "an earlier answer" not in state
    # models are described by registry facts, under opaque ids
    assert "m1: model gemma-4-12b; strength standard; cost free; context window 32768; capabilities chat, coding. Local 12B, fast." in state
    assert "m3: model anthropic/claude-sonnet; strength frontier; cost paid" in state
    # provider, credential and secret locations never leave
    for private in ("edgebox", "openrouter/", "main", "decisions", "EDGE_KEY", "OPENROUTER_API_KEY", "or-key"):
        assert private not in state, private


@pytest.mark.asyncio
async def test_full_conversation_and_metadata_inputs_are_opt_in(monkeypatch, tmp_path):
    app, decisions, _ = make_app(monkeypatch, tmp_path, raw=jev_raw(jev={"input": "full_conversation"}))
    await call(app)
    assert SECRET_TURN in decisions.body["state"] and LATEST in decisions.body["state"]

    app, decisions, _ = make_app(monkeypatch, tmp_path, raw=jev_raw(jev={"input": "metadata"}))
    await call(app)
    state = decisions.body["state"]
    assert SECRET_TURN not in state and LATEST not in state and "an earlier answer" not in state
    assert "turns: 3 (user turns: 2)" in state


@pytest.mark.asyncio
async def test_the_decision_input_is_truncated_to_policy(monkeypatch, tmp_path):
    app, decisions, _ = make_app(monkeypatch, tmp_path, raw=jev_raw(jev={"max_input_chars": 10}))
    await call(app, {**REQ, "messages": [{"role": "user", "content": "0123456789-overflow-not-sent"}]})
    state = decisions.body["state"]
    assert "0123456789 …[truncated]" in state and "overflow-not-sent" not in state


# ---- the choice reorders; Cerberus executes ----


@pytest.mark.asyncio
async def test_the_chosen_model_runs_first_through_cerberus(monkeypatch, tmp_path):
    app, _, upstream = make_app(monkeypatch, tmp_path, decide=chooses("m3"))
    resp, events = await call(app)

    assert resp.status_code == 200
    assert upstream.models() == ["anthropic/claude-sonnet"]
    assert str(upstream.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert upstream.requests[0].headers["authorization"] == "Bearer or-key", "the pool member's credential"
    assert resp.json()["cerberus"]["jev"]["choice"] == "openrouter/anthropic/claude-sonnet"

    event = events[0]
    assert (event["mode"], event["outcome"], event["model"], event["cost_tier"]) == (
        "jev", "success", "anthropic/claude-sonnet", "paid"
    )
    assert event["used_fallback"] is False
    assert event["jev"] == {
        "decider": "openrouter",
        "model": "typesafe/jev-1.13",
        "input": "last_user_message",
        "options": ["edgebox/gemma-4-12b", "openrouter/vendor/mid", "openrouter/anthropic/claude-sonnet"],
        "decision": "chosen",
        "reason": None,
        "choice": "openrouter/anthropic/claude-sonnet",
        "confidence": 0.91,
        "latency_ms": event["jev"]["latency_ms"],
        "http_status": 200,
        "usage": {"prompt_tokens": 120, "completion_tokens": 1, "total_tokens": 121, "cost": 0.00004},
    }


@pytest.mark.asyncio
async def test_a_local_model_can_be_the_choice(monkeypatch, tmp_path):
    app, _, upstream = make_app(monkeypatch, tmp_path, decide=chooses("m1"))
    resp, _ = await call(app)
    assert upstream.models() == ["gemma-4-12b"]
    assert str(upstream.requests[0].url) == "http://edgebox.test/v1/chat/completions"
    assert resp.json()["choices"][0]["message"]["content"] == "answer from gemma-4-12b"


@pytest.mark.asyncio
async def test_a_failing_choice_fails_over_to_the_pool_in_config_order(monkeypatch, tmp_path):
    def frontier_rate_limited(request):
        if json.loads(request.content)["model"] == "anthropic/claude-sonnet":
            return httpx.Response(429, json={"error": "rate"})
        return generation_ok(request)

    app, _, upstream = make_app(monkeypatch, tmp_path, decide=chooses("m3"), generate=frontier_rate_limited)
    resp, events = await call(app)

    assert resp.status_code == 200
    assert upstream.models() == ["anthropic/claude-sonnet", "gemma-4-12b"]
    assert events[0]["used_fallback"] is True
    assert events[0]["jev"]["decision"] == "chosen"


@pytest.mark.asyncio
async def test_jev_aliases_stream_like_any_dispatch_alias(monkeypatch, tmp_path):
    def sse(request):
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
                              headers={"content-type": "text/event-stream"})

    app, decisions, upstream = make_app(monkeypatch, tmp_path, decide=chooses("m2"), generate=sse)
    resp, events = await call(app, {**REQ, "stream": True})
    assert resp.status_code == 200 and resp.headers["x-cerberus-model"] == "vendor/mid"
    assert len(decisions.requests) == 1
    assert events[0]["streaming"] is True and events[0]["jev"]["choice"] == "openrouter/vendor/mid"


# ---- no usable answer: configured order ----


def _raise(exc):
    def respond(request):
        raise exc("down", request=request)

    return respond


@pytest.mark.parametrize(
    ("decide", "reason", "status"),
    [
        (answer({"error": "boom"}, status=500), "http_error", 500),
        (answer({"error": "bad request"}, status=400), "http_error", 400),
        (_raise(httpx.ReadTimeout), "timeout", None),
        (_raise(httpx.ConnectError), "unreachable", None),
        (lambda r: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
         "invalid_response", 200),
        (chooses("m9"), "choice_outside_pool", 200),
        # a provider/model name is not an option id, however plausible
        (chooses("anthropic/claude-sonnet"), "choice_outside_pool", 200),
        (answer({"answers": {}, "usage": {}}), "no_choice", 200),
    ],
    ids=["http-500", "http-400", "timeout", "unreachable", "invalid-json", "unknown-id", "model-name", "no-answer"],
)
@pytest.mark.asyncio
async def test_without_a_usable_answer_the_request_runs_in_config_order(
    monkeypatch, tmp_path, decide, reason, status
):
    app, _, upstream = make_app(monkeypatch, tmp_path, decide=decide)
    resp, events = await call(app)

    assert resp.status_code == 200, "a decision failure never fails the request"
    assert upstream.models() == ["gemma-4-12b"], "configured order: the first pool model"
    record = events[0]["jev"]
    assert (record["decision"], record["reason"], record["http_status"]) == ("fallback", reason, status)
    assert record["choice"] is None and record["confidence"] is None


@pytest.mark.asyncio
async def test_a_missing_decider_credential_never_calls_the_decision_service(monkeypatch, tmp_path):
    app, decisions, upstream = make_app(monkeypatch, tmp_path, decider_key=None)
    resp, events = await call(app)
    assert resp.status_code == 200
    assert decisions.requests == []
    assert upstream.models() == ["gemma-4-12b"]
    assert (events[0]["jev"]["decision"], events[0]["jev"]["reason"]) == ("fallback", "credential_missing")


@pytest.mark.asyncio
async def test_with_one_model_left_no_decision_is_asked(monkeypatch, tmp_path):
    """Nothing to choose between, so no request text leaves for a decision."""
    app, decisions, upstream = make_app(monkeypatch, tmp_path)
    for model in ("vendor/mid", "anthropic/claude-sonnet"):
        app.state.cooldowns.apply(scope="model", provider="openrouter", credential="main", model=model,
                                  reason="quota_429", duration_seconds=600)
    resp, events = await call(app)

    assert resp.status_code == 200
    assert decisions.requests == []
    assert upstream.models() == ["gemma-4-12b"]
    record = events[0]["jev"]
    assert (record["decision"], record["reason"], record["options"]) == ("skipped", "single_option", ["edgebox/gemma-4-12b"])


@pytest.mark.asyncio
async def test_a_cooled_down_model_is_never_offered(monkeypatch, tmp_path):
    app, decisions, _ = make_app(monkeypatch, tmp_path, decide=chooses("m1"))
    app.state.cooldowns.apply(scope="model", provider="openrouter", credential="main", model="vendor/mid",
                              reason="quota_429", duration_seconds=600)
    await call(app)
    assert decisions.body["questions"][0]["options"] == ["m1", "m2"]
    assert "vendor/mid" not in decisions.body["state"]
    assert "m2: model anthropic/claude-sonnet" in decisions.body["state"]


@pytest.mark.asyncio
async def test_an_empty_pool_is_exhausted_without_a_decision(monkeypatch, tmp_path):
    app, decisions, upstream = make_app(monkeypatch, tmp_path, raw=jev_raw(candidates=(MID, FRONTIER)))
    app.state.cooldowns.apply(scope="credential", provider="openrouter", credential="main", model=None,
                              reason="quota_429", duration_seconds=600)
    resp, events = await call(app)
    assert resp.status_code == 503
    assert decisions.requests == [] and upstream.requests == []
    assert events[0]["outcome"] == "routing_exhausted"
    assert (events[0]["jev"]["decision"], events[0]["jev"]["reason"]) == ("skipped", "no_option")


# ---- the trust boundary ----


@pytest.mark.asyncio
async def test_no_request_text_reaches_telemetry(monkeypatch, tmp_path):
    app, _, _ = make_app(monkeypatch, tmp_path, raw=jev_raw(jev={"input": "full_conversation"}))
    _, events = await call(app)
    serialized = json.dumps(events)
    assert LATEST not in serialized and SECRET_TURN not in serialized


@pytest.mark.asyncio
async def test_server_tools_are_refused_before_any_decision(monkeypatch, tmp_path):
    app, decisions, upstream = make_app(monkeypatch, tmp_path)
    resp, _ = await call(app, {**REQ, "tools": [{"type": "openrouter:fusion", "parameters": {}}]})
    assert resp.status_code == 400
    assert decisions.requests == [] and upstream.requests == []


# ---- admin projection and control-plane records ----


@pytest.mark.asyncio
async def test_routes_project_the_pool_and_the_decider_separately(monkeypatch, tmp_path):
    app, _, _ = make_app(monkeypatch, tmp_path, decider_key=None)
    app.state.cooldowns.apply(scope="model", provider="openrouter", credential="main", model="vendor/mid",
                              reason="quota_429", duration_seconds=600)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            projection = (await client.get("/admin/routes")).json()

    entry = next(a for a in projection["aliases"] if a["alias"] == "cerberus/smart")
    jev = entry["jev"]
    assert [(m["provider"], m["model"], m["state"]) for m in jev["pool"]] == [
        ("edgebox", "gemma-4-12b", "eligible"),
        ("openrouter", "vendor/mid", "excluded"),
        ("openrouter", "anthropic/claude-sonnet", "eligible"),
    ]
    assert jev["readiness"] == {"pool_available": 2, "pool_size": 3, "available": True}
    # no decision can be had, and the alias still routes in configured order
    assert jev["decider"]["readiness"] == {
        "backend_present": True, "credential_present": False, "provider_available": True, "decides": False,
    }
    assert (jev["decider"]["model"], jev["decider"]["input"]) == ("typesafe/jev-1.13", "last_user_message")
    assert "JEV_DECISIONS_KEY" not in json.dumps(projection)


@pytest.mark.asyncio
async def test_revision_materializes_the_jev_pool(monkeypatch, tmp_path):
    import sqlite3

    state = tmp_path / "state.sqlite3"
    app, _, _ = make_app(monkeypatch, tmp_path, raw=jev_raw(state_path=state))
    async with app.router.lifespan_context(app):
        pass
    connection = sqlite3.connect(state)
    try:
        binding = connection.execute("SELECT binding_type FROM alias_bindings WHERE alias='cerberus/smart'").fetchone()
        roles = connection.execute("SELECT DISTINCT role FROM route_members WHERE alias='cerberus/smart'").fetchall()
    finally:
        connection.close()
    assert binding == ("jev",) and roles == [("pool",)]


# ---- backend unit: wire shape and tolerant answer reading ----


def test_decider_builds_the_documented_body():
    request = DecisionRequest(endpoint="https://openrouter.ai/api/alpha/decisions", api_key="k",
                              model="typesafe/jev-1.13", state="s", options=["m1", "m2"], timeout_seconds=5)
    body = OpenRouterJevDecider.build_body(request)
    assert body["model"] == "typesafe/jev-1.13" and body["state"] == "s"
    assert body["questions"][0]["options"] == ["m1", "m2"] and body["questions"][0]["type"] == "choice"


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({"route": {"choice": "m2", "probabilities": {"m1": 0.2, "m2": 0.8}}}, ("m2", 0.8)),
        ({"route": {"result": "m1"}}, ("m1", None)),
        ({"route": {"value": "m1", "confidence": 0.7}}, ("m1", 0.7)),
        ({"route": "m2"}, ("m2", None)),
        ([{"id": "route", "type": "choice", "result": "m2"}], ("m2", None)),
        ({"route": {"probabilities": {"m1": 0.1, "m2": 0.9}}}, ("m2", 0.9)),
        ({"other": {"choice": "m1"}}, (None, None)),
        ("m1", (None, None)),
        (None, (None, None)),
    ],
)
def test_the_route_answer_is_read_tolerantly(answers, expected):
    assert _route_answer(answers) == expected


@pytest.mark.asyncio
async def test_a_plan_cannot_add_a_target_the_alias_does_not_configure(monkeypatch, tmp_path):
    from dataclasses import replace

    from cerberus.router.dispatch import DispatchPlan, dispatch
    from cerberus.router.engine import ordered_targets

    app, _, upstream = make_app(monkeypatch, tmp_path)
    document = app.state.lifecycle.active
    stranger = replace(ordered_targets(document.config, "cerberus/smart")[0], model="elsewhere/unlisted")
    with pytest.raises(ValueError, match="does not configure"):
        await dispatch(
            body=REQ, alias_name="cerberus/smart", document=document, store=app.state.cooldowns,
            client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)), telemetry=None,
            plan=DispatchPlan(targets=[stranger], exclusions=[], jev={}),
        )
    assert upstream.requests == []
