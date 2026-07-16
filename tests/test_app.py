import httpx
import pytest

from metarouter.app import create_app
from metarouter.config import RouterConfig


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> RouterConfig:
    monkeypatch.setenv("PRIMARY_KEY", "primary-secret")
    monkeypatch.setenv("SECONDARY_KEY", "secondary-secret")
    return RouterConfig.model_validate(
        {
            "providers": {
                "primary": {
                    "base_url": "https://primary.example/v1",
                    "api_key_env": "PRIMARY_KEY",
                    "model": "primary-model",
                    "cooldown_seconds": 60,
                },
                "secondary": {
                    "base_url": "https://secondary.example/v1",
                    "api_key_env": "SECONDARY_KEY",
                    "model": "secondary-model",
                    "cooldown_seconds": 60,
                },
            },
            "pools": {
                "coding": {"strategy": "round_robin", "providers": ["primary", "secondary"]},
                "default": {"strategy": "first", "providers": ["secondary"]},
            },
            "routing_rules": [
                {"match": {"request_type": "coding"}, "pool": "coding", "fallback_pool": "default"},
                {"match": {"default": True}, "pool": "default"},
            ],
        }
    )


@pytest.mark.asyncio
async def test_routes_and_redacts_credentials(config: RouterConfig) -> None:
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(config, httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "caller-choice", "messages": [], "request_type": "coding"},
            )

    assert response.status_code == 200
    assert response.json()["metarouter"]["provider"] == "primary"
    assert response.json()["metarouter"]["model"] == "primary-model"
    assert "primary-secret" not in response.text
    assert seen[0].url == "https://primary.example/v1/chat/completions"
    upstream_body = httpx.Response(200, content=seen[0].content).json()
    assert upstream_body["model"] == "primary-model"
    assert "request_type" not in upstream_body
    assert seen[0].headers["authorization"] == "Bearer primary-secret"


@pytest.mark.asyncio
async def test_root_redirects_to_interactive_api_docs(config: RouterConfig) -> None:
    app = create_app(config, httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
        ) as client:
            response = await client.get("/")

    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


@pytest.mark.asyncio
async def test_rate_limit_fails_over_to_next_provider(config: RouterConfig) -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.example":
            return httpx.Response(429, headers={"retry-after": "10"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fallback"}}]})

    app = create_app(config, httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={"messages": [], "request_type": "coding"})
            health = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["metarouter"]["provider"] == "secondary"
    assert response.json()["metarouter"]["attempts"] == 2
    assert health.json()["cooldowns"]["primary"] > 0


@pytest.mark.asyncio
async def test_streaming_response_is_proxied(config: RouterConfig) -> None:
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: hello\n\ndata: [DONE]\n\n"
        )

    app = create_app(config, httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={"messages": [], "stream": True})

    assert response.status_code == 200
    assert response.headers["x-metarouter-provider"] == "secondary"
    assert response.text == "data: hello\n\ndata: [DONE]\n\n"


@pytest.mark.asyncio
async def test_malformed_json_returns_client_error_without_calling_upstream(config: RouterConfig) -> None:
    calls = 0

    async def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    app = create_app(config, httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                content=b'{"messages":',
                headers={"content-type": "application/json"},
            )

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "Invalid JSON body"}}
    assert calls == 0


def test_credential_validation_uses_names_only(monkeypatch: pytest.MonkeyPatch, config: RouterConfig) -> None:
    monkeypatch.delenv("PRIMARY_KEY")
    with pytest.raises(RuntimeError, match="PRIMARY_KEY") as exc_info:
        config.require_configured_credentials()
    assert "primary-secret" not in str(exc_info.value)


def test_external_bind_requires_api_token() -> None:
    with pytest.raises(ValueError, match="api_token_env"):
        RouterConfig.model_validate(
            {
                "server": {"host": "0.0.0.0"},
                "providers": {"local": {"base_url": "http://127.0.0.1:8080/v1", "model": "local"}},
                "pools": {"default": {"providers": ["local"]}},
                "routing_rules": [{"match": {"default": True}, "pool": "default"}],
            }
        )


@pytest.mark.asyncio
async def test_external_bind_rejects_unauthenticated_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METAROUTER_API_TOKEN", "router-token")
    config = RouterConfig.model_validate(
        {
            "server": {"host": "0.0.0.0", "api_token_env": "METAROUTER_API_TOKEN"},
            "providers": {"local": {"base_url": "http://127.0.0.1:8080/v1", "model": "local"}},
            "pools": {"default": {"providers": ["local"]}},
            "routing_rules": [{"match": {"default": True}, "pool": "default"}],
        }
    )
    app = create_app(config, httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            unauthorized = await client.get("/v1/models")
            authorized = await client.get("/v1/models", headers={"authorization": "Bearer router-token"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
