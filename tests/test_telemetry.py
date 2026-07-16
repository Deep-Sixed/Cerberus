import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from metarouter.app import create_app
from metarouter.config import RouterConfig
from metarouter.telemetry import RoutingEvent, TelemetryEmitter


def telemetry_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> RouterConfig:
    monkeypatch.setenv("PRIMARY_KEY", "provider-test-value-primary")
    monkeypatch.setenv("SECONDARY_KEY", "provider-test-value-secondary")
    token_file = tmp_path / "contextforge-telemetry-token"
    token_file.write_text("telemetry-test-value", encoding="utf-8")
    return RouterConfig.model_validate(
        {
            "telemetry": {
                "endpoint": "http://contextforge.test/v1/telemetry/routing-records",
                "bearer_token_file": str(token_file),
                "timeout_seconds": 0.2,
            },
            "providers": {
                "primary": {
                    "base_url": "https://primary.example/v1",
                    "api_key_env": "PRIMARY_KEY",
                    "model": "primary-model",
                },
                "secondary": {
                    "base_url": "https://secondary.example/v1",
                    "api_key_env": "SECONDARY_KEY",
                    "model": "secondary-model",
                },
            },
            "pools": {
                "primary": {"providers": ["primary"]},
                "fallback": {"providers": ["secondary"]},
            },
            "routing_rules": [
                {"match": {"request_type": "controlled"}, "pool": "primary", "fallback_pool": "fallback"},
                {"match": {"default": True}, "pool": "primary", "fallback_pool": "fallback"},
            ],
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
            "messages": [{"role": "user", "content": "prompt-sensitive-value"}],
            "request_type": "controlled",
        },
    )

    assert response.status_code == 200
    assert len(events) == 1
    assert events[0].headers["authorization"] == "Bearer telemetry-test-value"
    event = json.loads(events[0].content)
    assert event["schema_version"] == 1
    assert event["provider"] == "primary"
    assert event["pool"] == "primary"
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
async def test_unknown_request_type_is_normalized_before_telemetry(monkeypatch, tmp_path) -> None:
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
            for request_type in unsafe_values:
                response = await client.post(
                    "/v1/chat/completions", json={"messages": [], "request_type": request_type}
                )
                assert response.status_code == 200

    assert [event["request_type"] for event in events] == ["default", "default"]
    serialized = json.dumps(events)
    for unsafe_value in unsafe_values:
        assert unsafe_value not in serialized


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
    response = await _request(app, {"messages": [], "request_type": "controlled"})

    assert response.status_code == 200
    assert response.json()["metarouter"]["provider"] == "secondary"
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "secondary"
    assert event["pool"] == "fallback"
    assert event["used_fallback"] is True
    assert event["attempt_count"] == 2
    assert event["attempts"][0]["provider"] == "primary"
    assert event["attempts"][0]["outcome"] == "retryable_status"
    assert event["attempts"][0]["http_status"] == 429
    assert event["attempts"][1]["provider"] == "secondary"
    assert event["attempts"][1]["outcome"] == "response"


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
    response = await _request(app, {"messages": [], "stream": True, "request_type": "controlled"})

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
                    "/v1/chat/completions", json={"messages": [], "stream": True, "request_type": "controlled"}
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
    response = await _request(app, {"messages": [], "request_type": "controlled"})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_bounded_telemetry_queue_drops_new_events_when_full(monkeypatch, tmp_path) -> None:
    config = telemetry_config(monkeypatch, tmp_path)
    config.telemetry = config.telemetry.model_copy(update={"queue_capacity": 1})
    emitter = TelemetryEmitter(config.telemetry, httpx.MockTransport(lambda _request: httpx.Response(201)))
    event = RoutingEvent(
        request_id="123e4567-e89b-42d3-a456-426614174000",
        request_type="controlled",
        provider="primary",
        pool="primary",
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
    response = await _request(app, {"messages": [], "request_type": "controlled"})

    assert response.status_code == 502
    assert len(events) == 1
    assert events[0]["http_status"] == 502
    assert events[0]["outcome"] == "upstream_error"
    assert events[0]["attempts"][-1]["outcome"] == "invalid_response"
    assert "RAW_RESPONSE_MUST_NOT_PERSIST" not in json.dumps(events[0])


def test_telemetry_configuration_requires_endpoint_and_token_file_together() -> None:
    with pytest.raises(ValueError, match="configured together"):
        RouterConfig.model_validate(
            {
                "telemetry": {"endpoint": "http://contextforge.test/v1/telemetry/routing-records"},
                "providers": {"local": {"base_url": "http://localhost:8080/v1", "model": "local"}},
                "pools": {"default": {"providers": ["local"]}},
                "routing_rules": [{"match": {"default": True}, "pool": "default"}],
            }
        )


def test_routing_rule_rejects_oversized_request_type() -> None:
    with pytest.raises(ValueError, match="request_type"):
        RouterConfig.model_validate(
            {
                "providers": {"local": {"base_url": "http://localhost:8080/v1", "model": "local"}},
                "pools": {"default": {"providers": ["local"]}},
                "routing_rules": [
                    {"match": {"request_type": "x" * 101}, "pool": "default"},
                    {"match": {"default": True}, "pool": "default"},
                ],
            }
        )
