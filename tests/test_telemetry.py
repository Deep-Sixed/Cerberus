"""Telemetry behavior — redaction, usage capture, bounded queue, non-blocking emission.

Routing scenarios exercise the Cerberus event contract, redaction boundary and
snapshot attribution.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import CerberusConfig
from cerberus.telemetry import RoutingEvent, TelemetryEmitter


def telemetry_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> CerberusConfig:
    monkeypatch.setenv("PRIMARY_KEY", "provider-test-value-primary")
    monkeypatch.setenv("SECONDARY_KEY", "provider-test-value-secondary")
    token_file = tmp_path / "contextforge-telemetry-token"
    token_file.write_text("telemetry-test-value", encoding="utf-8")
    return CerberusConfig.model_validate(
        {
            "metadata": {"version": "cerberus-2026-07-16.1"},
            "telemetry": {
                "endpoint": "http://contextforge.test/v1/telemetry/routing-records",
                "bearer_token_file": str(token_file),
                "timeout_seconds": 0.2,
            },
            "providers": {
                "primary": {
                    "base_url": "https://primary.example/v1",
                    "credentials": {"main": {"api_key_env": "PRIMARY_KEY"}},
                    "models": {"primary-model": {"cost_tier": "free"}},
                },
                "secondary": {
                    "base_url": "https://secondary.example/v1",
                    "credentials": {"main": {"api_key_env": "SECONDARY_KEY"}},
                    "models": {"secondary-model": {"cost_tier": "free"}},
                },
            },
            "aliases": {
                "cerberus/controlled": {
                    "mode": "dispatch",
                    "candidates": [
                        {"provider": "primary", "credential": "main", "model": "primary-model"},
                        {"provider": "secondary", "credential": "main", "model": "secondary-model"},
                    ],
                },
            },
        }
    )


async def _request(app, body: dict) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/chat/completions", json=body)


@pytest.mark.asyncio
async def test_non_streaming_event_is_redacted_and_includes_usage(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[httpx.Request] = []

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "response-sensitive-value"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(request)
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    response = await _request(
        app,
        {
            "model": "cerberus/controlled",
            "messages": [{"role": "user", "content": "prompt-sensitive-value"}],
        },
    )

    assert response.status_code == 200
    assert len(events) == 1
    assert events[0].headers["authorization"] == "Bearer telemetry-test-value"
    event = json.loads(events[0].content)
    assert event["schema_version"] == 5
    assert event["alias"] == "cerberus/controlled"
    assert event["attempts"][0]["used_fallback"] is False
    assert event["attempts"][0]["cooldown_scope"] is None
    assert event["provider"] == "primary"
    assert event["mode"] == "dispatch"
    assert event["model"] == "primary-model"
    assert event["attempt_count"] == 1
    assert event["used_fallback"] is False
    assert event["http_status"] == 200
    assert event["outcome"] == "success"
    assert event["latency_ms"] >= 0
    assert event["token_usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    serialized = json.dumps(event)
    for forbidden in (
        "prompt-sensitive-value",
        "response-sensitive-value",
        "telemetry-test-value",
        "provider-test-value-primary",
        "authorization",
        "messages",
        "choices",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_unknown_alias_emits_no_event_and_no_caller_text(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[dict] = []

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    unsafe_values = ("prompt-sensitive-value", "x" * 1_000)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for unsafe in unsafe_values:
                response = await client.post("/v1/chat/completions", json={"model": unsafe, "messages": []})
                assert response.status_code == 404

    assert events == []


@pytest.mark.asyncio
async def test_forced_fallback_records_both_attempts(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[dict] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.example":
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fallback"}}]})

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    response = await _request(app, {"model": "cerberus/controlled", "messages": []})

    assert response.status_code == 200
    assert response.json()["cerberus"]["provider"] == "secondary"
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "secondary"
    assert event["used_fallback"] is True
    assert event["attempt_count"] == 2
    assert event["attempts"][0]["provider"] == "primary/main"
    assert event["attempts"][0]["outcome"] == "retryable_status"
    assert event["attempts"][0]["http_status"] == 429
    # the first-choice attempt is not a fallback; the 429 recorded the exact applied scope
    assert event["attempts"][0]["used_fallback"] is False
    assert event["attempts"][0]["cooldown_scope"] == "model"
    assert event["attempts"][1]["provider"] == "secondary/main"
    assert event["attempts"][1]["outcome"] == "response"
    # the successful second attempt is explicitly a fallback, agreeing with the event
    assert event["attempts"][1]["used_fallback"] is True
    assert event["attempts"][1]["cooldown_scope"] is None


@pytest.mark.asyncio
async def test_streaming_event_is_emitted_with_usage(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[dict] = []

    async def upstream(_request: httpx.Request) -> httpx.Response:
        content = (
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    response = await _request(app, {"model": "cerberus/controlled", "messages": [], "stream": True})

    assert response.status_code == 200
    assert len(events) == 1
    assert events[0]["streaming"] is True
    assert events[0]["token_usage"] == {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}


@pytest.mark.asyncio
async def test_interrupted_stream_marks_final_attempt(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[dict] = []

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            raise RuntimeError("controlled stream interruption")

        async def aclose(self) -> None:
            return None

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=InterruptedStream())

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with pytest.raises(RuntimeError, match="controlled stream interruption"):
                await client.post(
                    "/v1/chat/completions",
                    json={"model": "cerberus/controlled", "messages": [], "stream": True},
                )

    assert len(events) == 1
    assert events[0]["outcome"] == "stream_interrupted"
    assert events[0]["attempts"][-1]["outcome"] == "stream_interrupted"


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_fail_or_delay_inference(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def telemetry(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("controlled telemetry outage", request=request)

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    started = time.perf_counter()
    response = await _request(app, {"model": "cerberus/controlled", "messages": []})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_http_auth_failure_degrades_telemetry_without_failing_liveness(
    monkeypatch, tmp_path, caplog
) -> None:
    config = telemetry_config(monkeypatch, tmp_path)

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def telemetry(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid authentication credentials"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    with caplog.at_level(logging.ERROR, logger="cerberus.telemetry.emitter"):
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                inference = await client.post(
                    "/v1/chat/completions",
                    json={"model": "cerberus/controlled", "messages": []},
                )
                for _ in range(20):
                    health = await client.get("/admin/health")
                    if health.json()["telemetry"]["status"] == "degraded":
                        break
                    await asyncio.sleep(0)

    assert inference.status_code == 200
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "ok"
    assert payload["routing"] == {"status": "healthy"}
    assert payload["telemetry"]["last_error"] == "http_401"
    assert payload["telemetry"]["last_status_code"] == 401
    assert payload["telemetry"]["consecutive_failures"] == 1
    assert "status_code=401" in caplog.text
    assert "telemetry-test-value" not in caplog.text


@pytest.mark.asyncio
async def test_transport_failure_is_distinct_and_success_clears_degradation(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    responses = ["transport_error", "success"]

    async def sink(request: httpx.Request) -> httpx.Response:
        if responses.pop(0) == "transport_error":
            raise httpx.ConnectError("controlled outage", request=request)
        return httpx.Response(201)

    emitter = TelemetryEmitter(config.telemetry, httpx.MockTransport(sink), release_id="test-release")
    await emitter.start()
    emitter.emit(_minimal_event())
    while emitter.health_snapshot()["status"] == "pending":
        await asyncio.sleep(0)

    degraded = emitter.health_snapshot()
    assert degraded["status"] == "degraded"
    assert degraded["last_error"] == "transport_error"
    assert degraded["last_status_code"] is None
    assert degraded["consecutive_failures"] == 1

    emitter.emit(_minimal_event(request_id="00000000-0000-4000-8000-000000000002"))
    await emitter.close()

    recovered = emitter.health_snapshot()
    assert recovered["status"] == "healthy"
    assert recovered["last_error"] is None
    assert recovered["consecutive_failures"] == 0
    assert recovered["last_success_at"] is not None


@pytest.mark.asyncio
async def test_bounded_telemetry_queue_drops_new_events_when_full(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    telemetry_settings = config.telemetry.model_copy(update={"queue_capacity": 1})
    emitter = TelemetryEmitter(
        telemetry_settings, httpx.MockTransport(lambda _request: httpx.Response(201)), release_id="test-release"
    )
    event = RoutingEvent(
        request_id="123e4567-e89b-42d3-a456-426614174000",
        alias="cerberus/controlled",
        provider="primary",
        mode="dispatch",
        model="primary-model",
        used_fallback=False,
        attempts=[],
        http_status=200,
        outcome="success",
        latency_ms=1.0,
        token_usage=None,
        timestamp=datetime.now(timezone.utc),
        streaming=False,
    )

    await emitter.start()
    emitter.emit(event)
    emitter.emit(event)

    assert emitter.dropped_events == 1
    await emitter.close()


@pytest.mark.asyncio
async def test_invalid_upstream_json_emits_redacted_failure_event(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[dict] = []

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"RAW_RESPONSE_MUST_NOT_PERSIST")

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    response = await _request(app, {"model": "cerberus/controlled", "messages": []})

    assert response.status_code == 502
    assert len(events) == 1
    assert events[0]["http_status"] == 502
    assert events[0]["outcome"] == "upstream_error"
    assert events[0]["attempts"][-1]["outcome"] == "invalid_response"
    assert "RAW_RESPONSE_MUST_NOT_PERSIST" not in json.dumps(events[0])


def test_telemetry_configuration_requires_endpoint_and_token_file_together() -> None:
    with pytest.raises(ValueError, match="configured together"):
        CerberusConfig.model_validate(
            {
                "metadata": {"version": "cerberus-2026-07-16.1"},
                "telemetry": {"endpoint": "http://contextforge.test/v1/telemetry/routing-records"},
                "providers": {
                    "local": {
                        "base_url": "http://localhost:8080/v1",
                        "credentials": {"main": {"api_key_env": "LOCAL_KEY"}},
                        "models": {"local": {"cost_tier": "free"}},
                    }
                },
                "aliases": {
                    "cerberus/free": {
                        "mode": "free",
                        "candidates": [{"provider": "local", "credential": "main", "model": "local"}],
                    }
                },
            }
        )


# -- Session 8 hardening: identity fields, snapshot semantics, drain ----------


def _minimal_event(**overrides) -> RoutingEvent:
    base = dict(
        request_id="123e4567-e89b-42d3-a456-426614174000",
        alias="cerberus/controlled",
        provider="primary",
        mode="dispatch",
        model="primary-model",
        used_fallback=False,
        attempts=[],
        http_status=200,
        outcome="success",
        latency_ms=1.0,
        token_usage=None,
        timestamp=datetime.now(timezone.utc),
        streaming=False,
        exclusions=[{"provider": "secondary", "reason": "cost_tier"}],
    )
    base.update(overrides)
    return RoutingEvent(**base)


def test_emitter_rejects_empty_release_id(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="release_id"):
        TelemetryEmitter(config.telemetry, release_id="")


def test_release_id_dev_fallback_is_deterministic_and_marked(monkeypatch) -> None:
    from cerberus.app import _release_id

    monkeypatch.delenv("CERBERUS_RELEASE_ID", raising=False)
    fallback = _release_id()
    assert fallback == _release_id()  # deterministic
    assert fallback.startswith("dev-")  # explicitly dev-marked
    monkeypatch.setenv("CERBERUS_RELEASE_ID", "release-sha256-abc")
    assert _release_id() == "release-sha256-abc"


def test_recent_events_snapshot_is_bounded_ordered_and_isolated(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    emitter = TelemetryEmitter(config.telemetry, release_id="test-release", recent_capacity=3)

    for index in range(5):
        emitter.emit(_minimal_event(request_id=f"00000000-0000-4000-8000-00000000000{index}"))

    snapshot = emitter.recent_events_snapshot()
    # bounded to capacity, newest first, oldest evicted
    assert [e["request_id"][-1] for e in snapshot] == ["4", "3", "2"]
    assert all(e["release_id"] == "test-release" for e in snapshot)
    # caller mutation of the returned value must not corrupt the store
    snapshot[0]["outcome"] = "tampered"
    snapshot[0]["exclusions"].append({"injected": True})
    fresh = emitter.recent_events_snapshot()
    assert fresh[0]["outcome"] == "success"
    assert fresh[0]["exclusions"] == [{"provider": "secondary", "reason": "cost_tier"}]


def test_recorded_event_is_immune_to_later_request_path_mutation(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    emitter = TelemetryEmitter(config.telemetry, release_id="test-release")
    exclusions = [{"provider": "secondary", "reason": "cost_tier"}]
    emitter.emit(_minimal_event(exclusions=exclusions))

    exclusions.append({"provider": "late", "reason": "mutated_after_emit"})
    exclusions[0]["reason"] = "rewritten"

    recorded = emitter.recent_events_snapshot()[0]
    assert recorded["exclusions"] == [{"provider": "secondary", "reason": "cost_tier"}]


@pytest.mark.asyncio
async def test_close_delivers_queued_events_before_shutdown(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    delivered: list[dict] = []

    async def sink(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    emitter = TelemetryEmitter(config.telemetry, httpx.MockTransport(sink), release_id="test-release")
    await emitter.start()
    for index in range(4):
        emitter.emit(_minimal_event(request_id=f"00000000-0000-4000-8000-00000000000{index}"))
    await emitter.close()

    assert len(delivered) == 4
    assert emitter.dropped_events == 0


@pytest.mark.asyncio
async def test_event_config_version_binds_to_decision_time_config(monkeypatch, tmp_path) -> None:
    """A config activation while a request is in flight must not relabel its event."""

    from tests.test_control import raw_config, write_config

    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    version_b = write_config(tmp_path, "v2.yaml", raw_config("cerberus-2026-07-16.2"))
    doc_holder: dict = {}

    async def upstream(_request: httpx.Request) -> httpx.Response:
        # mid-flight: operator activates config B while this request routes on A
        doc_holder["app"].state.lifecycle.activate(version_b)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    from cerberus.registry import load_config_document

    doc = load_config_document(write_config(tmp_path, "v1.yaml", raw_config("cerberus-2026-07-16.1")))
    app = create_app(doc, http_transport=httpx.MockTransport(upstream))
    doc_holder["app"] = app
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40001))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})
            health = await client.get("/admin/health")
            events = await client.get("/admin/events")

    assert response.status_code == 200
    assert health.json()["config_version"] == "cerberus-2026-07-16.2"  # B is live now...
    event = events.json()["events"][0]
    assert event["config_version"] == "cerberus-2026-07-16.1"  # ...but the event kept A
    assert event["release_id"]


# -- Codex repair pass: fallback flags, applied scope, shutdown accounting ----


@pytest.mark.asyncio
async def test_all_candidates_failing_marks_fallback_attempts(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[dict] = []

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    response = await _request(app, {"model": "cerberus/controlled", "messages": []})

    assert response.status_code == 503
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "routing_exhausted"
    assert [a["used_fallback"] for a in event["attempts"]] == [False, True]
    assert all(a["cooldown_scope"] == "model" for a in event["attempts"])


@pytest.mark.asyncio
async def test_streaming_fallback_attempt_is_marked(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[dict] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.example":
            return httpx.Response(429)
        content = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    response = await _request(app, {"model": "cerberus/controlled", "messages": [], "stream": True})

    assert response.status_code == 200
    assert len(events) == 1
    event = events[0]
    assert event["used_fallback"] is True
    assert event["provider"] == "secondary"
    assert event["attempts"][-1]["used_fallback"] is True
    assert event["attempts"][-1]["outcome"] == "response"


@pytest.mark.asyncio
async def test_interrupted_stream_after_fallback_keeps_fallback_flag(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    events: list[dict] = []

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            raise RuntimeError("controlled stream interruption")

        async def aclose(self) -> None:
            return None

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.example":
            return httpx.Response(429)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=InterruptedStream())

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with pytest.raises(RuntimeError, match="controlled stream interruption"):
                await client.post(
                    "/v1/chat/completions",
                    json={"model": "cerberus/controlled", "messages": [], "stream": True},
                )

    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "stream_interrupted"
    assert event["used_fallback"] is True
    assert event["attempts"][-1]["outcome"] == "stream_interrupted"
    assert event["attempts"][-1]["used_fallback"] is True


@pytest.mark.asyncio
async def test_credential_scoped_429_records_credential_scope(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRIMARY_KEY", "provider-test-value-primary")
    token_file = tmp_path / "token"
    token_file.write_text("telemetry-test-value", encoding="utf-8")
    config = CerberusConfig.model_validate(
        {
            "metadata": {"version": "cerberus-2026-07-16.1"},
            "telemetry": {
                "endpoint": "http://contextforge.test/v1/telemetry/routing-records",
                "bearer_token_file": str(token_file),
                "timeout_seconds": 0.2,
            },
            "providers": {
                "primary": {
                    "base_url": "https://primary.example/v1",
                    "quota_scope": "credential",
                    "credentials": {"main": {"api_key_env": "PRIMARY_KEY"}},
                    "models": {"primary-model": {"cost_tier": "free"}},
                },
            },
            "aliases": {
                "cerberus/controlled": {
                    "mode": "dispatch",
                    "candidates": [{"provider": "primary", "credential": "main", "model": "primary-model"}],
                },
            },
        }
    )
    events: list[dict] = []

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async def telemetry(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "event-id"})

    app = create_app(config, httpx.MockTransport(upstream), httpx.MockTransport(telemetry))
    body = {"model": "cerberus/controlled", "messages": []}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post("/v1/chat/completions", json=body)
            second = await client.post("/v1/chat/completions", json=body)

    assert first.status_code == 503 and second.status_code == 503
    assert len(events) == 2
    # the attempt records the exact scope the store received
    attempt = events[0]["attempts"][0]
    assert attempt["outcome"] == "retryable_status"
    assert attempt["cooldown_scope"] == "credential"
    # and the follow-up request is excluded by a cooldown with that same scope
    exclusion = events[1]["exclusions"][0]
    assert exclusion["reason"] == "cooldown_quota_429"
    assert exclusion["scope"] == "credential"


@pytest.mark.asyncio
async def test_drain_timeout_counts_every_discarded_event(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    entered = asyncio.Event()
    blocker = asyncio.Event()  # never set: transport blocks forever

    async def blocked_sink(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await blocker.wait()
        return httpx.Response(201)

    emitter = TelemetryEmitter(config.telemetry, httpx.MockTransport(blocked_sink), release_id="test-release")
    await emitter.start()
    for index in range(3):
        emitter.emit(_minimal_event(request_id=f"00000000-0000-4000-8000-00000000000{index}"))
    await asyncio.wait_for(entered.wait(), timeout=2)  # worker holds event 0 in delivery
    await emitter.close()

    # one in-flight + two still queued: exactly three discarded, counted individually
    assert emitter.dropped_events == 3


@pytest.mark.asyncio
async def test_events_delivered_before_drain_timeout_are_not_counted_dropped(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    delivered: list[dict] = []
    blocker = asyncio.Event()  # never set

    async def sink(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["request_id"].endswith("0"):
            delivered.append(payload)
            return httpx.Response(201)
        await blocker.wait()
        return httpx.Response(201)

    emitter = TelemetryEmitter(config.telemetry, httpx.MockTransport(sink), release_id="test-release")
    await emitter.start()
    for index in range(3):
        emitter.emit(_minimal_event(request_id=f"00000000-0000-4000-8000-00000000000{index}"))
    while not delivered:
        await asyncio.sleep(0.01)
    await emitter.close()

    assert len(delivered) == 1
    assert emitter.dropped_events == 2  # event 1 in-flight + event 2 queued; event 0 delivered
