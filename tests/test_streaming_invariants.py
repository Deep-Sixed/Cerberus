"""Acceptance tests for PR 2: Streaming and failover invariants.

Invariants verified:
1. Retryable upstream status before commitment -> fallback allowed.
2. Transport failure before commitment -> fallback allowed.
3. Stream fails after first emitted chunk -> no provider switch.
4. Interrupted stream closes upstream.
5. Interrupted stream emits terminal telemetry.
6. Selected request remains tied to its pinned configuration revision.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from cerberus.app import create_app
from cerberus.registry import CerberusConfig, load_config_document


def two_provider_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str = "cerberus-2026-09-15.1") -> dict:
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("BETA_KEY", "beta-secret")
    token_file = tmp_path / "telemetry-token"
    token_file.write_text("telemetry-secret", encoding="utf-8")
    return {
        "metadata": {"version": version},
        "server": {"host": "127.0.0.1", "port": 4000},
        "telemetry": {
            "endpoint": "http://telemetry.test/v1/events",
            "bearer_token_file": str(token_file),
            "timeout_seconds": 0.5,
        },
        "providers": {
            "alpha": {
                "base_url": "https://alpha.example/v1",
                "credentials": {"main": {"api_key_env": "ALPHA_KEY"}},
                "models": {"alpha-model": {"cost_tier": "free"}},
            },
            "beta": {
                "base_url": "https://beta.example/v1",
                "credentials": {"main": {"api_key_env": "BETA_KEY"}},
                "models": {"beta-model": {"cost_tier": "free"}},
            },
        },
        "aliases": {
            "cerberus/stream-alias": {
                "mode": "dispatch",
                "candidates": [
                    {"provider": "alpha", "credential": "main", "model": "alpha-model"},
                    {"provider": "beta", "credential": "main", "model": "beta-model"},
                ],
            },
        },
    }


class TrackedByteStream(httpx.AsyncByteStream):
    """An async byte stream that tracks closure and can fail mid-stream."""

    def __init__(self, chunks: list[bytes], *, fail_after: int | None = None) -> None:
        self._chunks = chunks
        self._fail_after = fail_after
        self.closed = False

    async def __aiter__(self):
        for index, chunk in enumerate(self._chunks):
            yield chunk
            if self._fail_after is not None and (index + 1) >= self._fail_after:
                raise RuntimeError("controlled mid-stream failure")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_retryable_status_before_commitment_allows_fallback(monkeypatch, tmp_path):
    """Invariant 1: A 429/retryable status before commitment triggers fallback to next candidate."""
    raw = two_provider_config(monkeypatch, tmp_path)
    config = CerberusConfig.model_validate(raw)
    calls: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "alpha.example":
            return httpx.Response(429, headers={"retry-after": "5"})
        stream = TrackedByteStream([b'data: {"choices":[{"delta":{"content":"fallback success"}}]}\n\n'])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    app = create_app(config, http_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "cerberus/stream-alias", "messages": [], "stream": True},
            )

    assert response.status_code == 200
    assert response.headers["x-cerberus-provider"] == "beta"
    assert response.headers["x-cerberus-model"] == "beta-model"
    assert "fallback success" in response.text
    assert calls == ["alpha.example", "beta.example"]


@pytest.mark.asyncio
async def test_transport_failure_before_commitment_allows_fallback(monkeypatch, tmp_path):
    """Invariant 2: A transport failure before commitment triggers fallback to next candidate."""
    raw = two_provider_config(monkeypatch, tmp_path)
    config = CerberusConfig.model_validate(raw)
    calls: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "alpha.example":
            raise httpx.ConnectError("connection refused", request=request)
        stream = TrackedByteStream([b'data: {"choices":[{"delta":{"content":"secondary stream"}}]}\n\n'])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    app = create_app(config, http_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "cerberus/stream-alias", "messages": [], "stream": True},
            )

    assert response.status_code == 200
    assert response.headers["x-cerberus-provider"] == "beta"
    assert "secondary stream" in response.text
    assert calls == ["alpha.example", "beta.example"]


@pytest.mark.asyncio
async def test_stream_fails_after_first_chunk_no_provider_switch(monkeypatch, tmp_path):
    """Invariant 3: After the first chunk is emitted, a stream failure NEVER switches providers."""
    raw = two_provider_config(monkeypatch, tmp_path)
    config = CerberusConfig.model_validate(raw)
    calls: list[str] = []

    stream = TrackedByteStream(
        [
            b'data: {"choices":[{"delta":{"content":"chunk 1"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"chunk 2"}}]}\n\n',
        ],
        fail_after=1,
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "alpha.example":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        return httpx.Response(200, json={"choices": [{"message": {"content": "should never be called"}}]})

    app = create_app(config, http_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        # With raise_app_exceptions=False, ASGITransport yields the body parts
        # emitted before the mid-stream failure, exactly reflecting what the HTTP client received.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "cerberus/stream-alias", "messages": [], "stream": True},
            )
            assert response.status_code == 200
            assert b"chunk 1" in response.content

    # The crucial invariant: only alpha was called; Cerberus NEVER attempted beta mid-stream
    assert calls == ["alpha.example"]


@pytest.mark.asyncio
async def test_interrupted_stream_closes_upstream(monkeypatch, tmp_path):
    """Invariant 4: An interrupted or broken stream invokes response.aclose() on upstream."""
    raw = two_provider_config(monkeypatch, tmp_path)
    config = CerberusConfig.model_validate(raw)

    stream = TrackedByteStream(
        [b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'],
        fail_after=1,
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    app = create_app(config, http_transport=httpx.MockTransport(upstream))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with pytest.raises(RuntimeError, match="controlled mid-stream failure"):
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={"model": "cerberus/stream-alias", "messages": [], "stream": True},
                ) as response:
                    async for _ in response.aiter_bytes():
                        pass

    # Upstream stream.aclose() was cleanly called in proxy finally block
    assert stream.closed is True


@pytest.mark.asyncio
async def test_interrupted_stream_emits_terminal_telemetry(monkeypatch, tmp_path):
    """Invariant 5: When a stream fails mid-flight, terminal telemetry with stream_interrupted is emitted."""
    raw = two_provider_config(monkeypatch, tmp_path)
    config = CerberusConfig.model_validate(raw)
    telemetry_events: list[dict] = []

    stream = TrackedByteStream(
        [b'data: {"choices":[{"delta":{"content":"streaming part"}}]}\n\n'],
        fail_after=1,
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async def sink(request: httpx.Request) -> httpx.Response:
        telemetry_events.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "ok"})

    app = create_app(
        config,
        http_transport=httpx.MockTransport(upstream),
        telemetry_transport=httpx.MockTransport(sink),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with pytest.raises(RuntimeError, match="controlled mid-stream failure"):
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={"model": "cerberus/stream-alias", "messages": [], "stream": True},
                ) as response:
                    async for _ in response.aiter_bytes():
                        pass

    assert len(telemetry_events) == 1
    event = telemetry_events[0]
    assert event["outcome"] == "stream_interrupted"
    assert event["streaming"] is True
    assert event["provider"] == "alpha"
    assert event["model"] == "alpha-model"
    assert event["used_fallback"] is False
    assert event["attempt_count"] == 1
    assert event["attempts"][-1]["outcome"] == "stream_interrupted"


@pytest.mark.asyncio
async def test_selected_request_remains_tied_to_pinned_configuration_revision(monkeypatch, tmp_path):
    """Invariant 6: An in-flight request pins its snapshot; runtime activation does not alter in-flight attribution."""
    v1_raw = two_provider_config(monkeypatch, tmp_path, version="cerberus-2026-09-15.1")
    v2_raw = two_provider_config(monkeypatch, tmp_path, version="cerberus-2026-09-15.2")

    v1_path = tmp_path / "v1.yaml"
    v2_path = tmp_path / "v2.yaml"
    v1_path.write_text(yaml.safe_dump(v1_raw), encoding="utf-8")
    v2_path.write_text(yaml.safe_dump(v2_raw), encoding="utf-8")

    doc1 = load_config_document(v1_path)
    telemetry_events: list[dict] = []

    stream_started = asyncio.Event()
    continue_stream = asyncio.Event()

    class SynchronizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"initial chunk"}}]}\n\n'
            stream_started.set()
            await continue_stream.wait()
            yield b'data: {"choices":[{"delta":{"content":"final chunk"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            pass

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SynchronizedStream())

    async def sink(request: httpx.Request) -> httpx.Response:
        telemetry_events.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "ok"})

    app = create_app(
        doc1,
        http_transport=httpx.MockTransport(upstream),
        telemetry_transport=httpx.MockTransport(sink),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def consumer():
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={"model": "cerberus/stream-alias", "messages": [], "stream": True},
                ) as response:
                    assert response.status_code == 200
                    chunks = []
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                    return chunks

            consumer_task = asyncio.create_task(consumer())

            # Wait until streaming begins and chunk 1 is yielded
            await stream_started.wait()

            # Mid-stream: hot-activate revision v2 in the runtime lifecycle
            activated = app.state.lifecycle.activate(str(v2_path))
            assert activated.version == "cerberus-2026-09-15.2"
            assert app.state.lifecycle.active.version == "cerberus-2026-09-15.2"

            # Allow the in-flight stream to finish
            continue_stream.set()
            result_chunks = await consumer_task

    assert any(b"final chunk" in c for c in result_chunks)
    assert len(telemetry_events) == 1
    event = telemetry_events[0]

    # Invariant: the in-flight request was pinned to v1 and its telemetry reflects v1, not v2
    assert event["config_version"] == "cerberus-2026-09-15.1"
    assert event["config_checksum"] == doc1.checksum
    assert event["outcome"] == "success"
