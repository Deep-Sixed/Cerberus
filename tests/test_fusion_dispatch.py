"""S11: fusion-mode dispatch fans out to the worker, re-emits telemetry, fails closed.

The worker is mocked at the HTTP boundary (fusion_transport). These prove the
Cerberus side: a fusion request reaches the worker and its panel/judge report is
re-emitted in the unified stream; a worker outage degrades only fusion aliases;
the alias's partial-failure policy is honored.
"""

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import load_config_document
from tests.test_control import ok_upstream, write_config


def fusion_raw(worker: bool = True) -> dict:
    raw = {
        "metadata": {"version": "cerberus-2026-07-17.2"},
        "providers": {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "credentials": {"main": {"api_key_env": "OPENROUTER_API_KEY"}},
                "models": {"free-a": {"cost_tier": "free"}, "free-b": {"cost_tier": "free"}},
            },
        },
        "identities": {
            "dev": {
                "credential_env": "CB_KEY_DEV",
                "allowed_modes": ["dispatch", "fusion"],
                "allowed_aliases": ["cerberus/dispatch-dev", "cerberus/fusion-dev"],
                "default_alias": "cerberus/dispatch-dev",
            },
        },
        "aliases": {
            "cerberus/dispatch-dev": {
                "mode": "dispatch",
                "candidates": [{"provider": "openrouter", "credential": "main", "model": "free-a"}],
            },
            "cerberus/fusion-dev": {
                "mode": "fusion",
                "candidates": [
                    {"provider": "openrouter", "credential": "main", "model": "free-a", "role": "Optimist."},
                    {"provider": "openrouter", "credential": "main", "model": "free-b", "role": "Skeptic."},
                ],
                "fusion": {
                    "max_panel_members": 5,
                    "timeout_seconds": 30,
                    "allow_paid_panel": False,
                    "judge": {"provider": "openrouter", "credential": "main", "model": "free-a"},
                    "on_partial_failure": "judge_with_partial",
                },
            },
        },
    }
    if worker:
        raw["fusion_worker"] = {
            "endpoint": "http://fusion-worker:4110",
            "bearer_token_env": "CERBERUS_FUSION_WORKER_TOKEN",
        }
    return raw


def worker_ok(report_failures: int = 0) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        panel = [
            {"label": "openrouter/free-a", "model": "free-a", "ok": True, "usage": None},
            {"label": "openrouter/free-b", "model": "free-b", "ok": True, "usage": None},
        ]
        for i in range(report_failures):
            panel[i] = {"label": panel[i]["label"], "ok": False, "reason": "boom", "status_code": 500}
        return httpx.Response(
            200,
            json={
                "id": "fusion_backend-1",
                "choices": [{"message": {"role": "assistant", "content": "synthesized answer"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "fusion_backend": {"panel": panel, "judge": {"model": "free-a", "usage": None}},
            },
        )

    return httpx.MockTransport(handler)


def make_app(monkeypatch, tmp_path, *, worker=True, fusion_transport=None):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("CB_KEY_DEV", "cb-dev")
    monkeypatch.setenv("CERBERUS_FUSION_WORKER_TOKEN", "worker-token")
    doc = load_config_document(write_config(tmp_path, "v.yaml", fusion_raw(worker=worker)))
    return create_app(
        doc,
        http_transport=httpx.MockTransport(ok_upstream),
        fusion_transport=fusion_transport,
    )


AUTH = {"authorization": "Bearer cb-dev"}
FUSION_REQ = {"model": "cerberus/fusion-dev", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_fusion_request_fans_out_and_re_emits_panel_judge(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, fusion_transport=worker_ok())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://t"
        ) as client:
            resp = await client.post("/v1/chat/completions", json=FUSION_REQ, headers=AUTH)
            events = (await client.get("/admin/events")).json()["events"]

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["choices"][0]["message"]["content"] == "synthesized answer"
    assert payload["cerberus"]["mode"] == "fusion"
    assert payload["cerberus"]["judge"] == "openrouter/free-a"
    # the internal worker report must not leak to the client
    assert "fusion_backend" not in payload
    # one unified event, mode=fusion, panel seats + judge as attempts
    event = events[0]
    assert event["mode"] == "fusion"
    assert event["outcome"] == "success"
    assert event["token_usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    pools = [a["pool"] for a in event["attempts"]]
    assert pools == ["panel", "panel", "judge"]
    assert event["candidates"] == ["openrouter/free-a", "openrouter/free-b"]


@pytest.mark.asyncio
async def test_worker_unreachable_fails_only_fusion(monkeypatch, tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("worker down", request=request)

    app = make_app(monkeypatch, tmp_path, fusion_transport=httpx.MockTransport(down))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://t"
        ) as client:
            fusion = await client.post("/v1/chat/completions", json=FUSION_REQ, headers=AUTH)
            # a dispatch alias on the SAME app still works while fusion is down
            dispatch = await client.post(
                "/v1/chat/completions",
                json={"model": "cerberus/dispatch-dev", "messages": [{"role": "user", "content": "hi"}]},
                headers=AUTH,
            )
            events = (await client.get("/admin/events")).json()["events"]

    assert fusion.status_code == 503
    assert dispatch.status_code == 200  # degradation is isolated to fusion
    fusion_event = next(e for e in events if e["mode"] == "fusion")
    assert fusion_event["outcome"] == "fusion_unavailable"


@pytest.mark.asyncio
async def test_not_configured_worker_fails_closed(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, worker=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://t"
        ) as client:
            resp = await client.post("/v1/chat/completions", json=FUSION_REQ, headers=AUTH)

    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_partial_failure_policy_fail_rejects(monkeypatch, tmp_path):
    raw = fusion_raw()
    raw["aliases"]["cerberus/fusion-dev"]["fusion"]["on_partial_failure"] = "fail"
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("CB_KEY_DEV", "cb-dev")
    monkeypatch.setenv("CERBERUS_FUSION_WORKER_TOKEN", "worker-token")
    doc = load_config_document(write_config(tmp_path, "v.yaml", raw))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream), fusion_transport=worker_ok(report_failures=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://t"
        ) as client:
            resp = await client.post("/v1/chat/completions", json=FUSION_REQ, headers=AUTH)
            events = (await client.get("/admin/events")).json()["events"]

    assert resp.status_code == 502
    assert events[0]["outcome"] == "upstream_error"


@pytest.mark.asyncio
async def test_worker_identity_cannot_call_a_fusion_alias(monkeypatch, tmp_path):
    """Recursion guard by authorization (SPEC §9): the worker's service identity
    excludes fusion mode, so it can never trigger a fusion alias — no headers."""
    raw = fusion_raw()
    # the worker's service identity excludes fusion mode. The schema enforces
    # deny-by-default: it may not even list a fusion alias, so a fusion call with
    # its key is denied — recursion prevention by authorization, not headers.
    raw["identities"]["fusion-worker"] = {
        "credential_env": "CB_KEY_WORKER",
        "allowed_modes": ["dispatch"],  # deliberately excludes "fusion"
        "allowed_aliases": ["cerberus/dispatch-dev"],
    }
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("CB_KEY_DEV", "cb-dev")
    monkeypatch.setenv("CB_KEY_WORKER", "cb-worker")
    monkeypatch.setenv("CERBERUS_FUSION_WORKER_TOKEN", "worker-token")
    doc = load_config_document(write_config(tmp_path, "v.yaml", raw))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream), fusion_transport=worker_ok())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://t"
        ) as client:
            worker_call = await client.post(
                "/v1/chat/completions", json=FUSION_REQ, headers={"authorization": "Bearer cb-worker"}
            )
            events = (await client.get("/admin/events")).json()["events"]

    assert worker_call.status_code == 403, "worker identity must be denied fusion by mode"
    assert events[0]["outcome"] == "unauthorized"
    assert events[0]["identity"] == "fusion-worker"
