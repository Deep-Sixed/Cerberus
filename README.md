# MetaRouter v3

MetaRouter v3 is a portable, policy-driven LLM router. It exposes an OpenAI
Chat Completions-compatible surface and selects a configured provider/model
without making ContextForge, EVECOR, or any particular host a runtime
dependency.

The initial provider runtime targets OpenAI-compatible upstreams. It was
designed from the provider-boundary lessons in `free-claude-code`, while keeping
v3's policy, credentials, and deployment ownership independent.

## Run

```bash
cp config/example.yaml config/metarouter.yaml
export OPENROUTER_API_KEY=...
uv run metarouter serve --config config/metarouter.yaml
```

The service listens on `127.0.0.1:4101` by default.

For a no-secret local smoke test, run `uv run metarouter serve --config
config/local.example.yaml`; it listens on `127.0.0.1:4111` and expects an
OpenAI-compatible local runtime at `:8080` only when a completion is requested.

## API

`GET /health` reports the configured service state.

`GET /v1/models` lists models configured by providers.

`POST /v1/chat/completions` accepts OpenAI-compatible requests. `request_type`
is a MetaRouter extension used only for policy matching; it is removed before
the request is sent upstream. Only configured routing-rule labels are retained.
Missing, malformed, oversized, or unknown values are normalized to `default`,
so untrusted request text is never recorded as telemetry.

## Portable Configuration

Each provider declares its endpoint, credential environment-variable name,
model, and cooldown policy. Pools choose providers with `first`, `random`, or
`round_robin`; rules select a pool by `request_type` and may name a fallback
pool. Credential values never appear in configuration or router metadata.

Cooldown state is process-local in v0.1. Use one router process per host or add
a shared state backend before horizontally scaling a single routing domain.

## Routing telemetry

MetaRouter is the authoritative producer for routing decisions. Telemetry is
optional and disabled unless both `telemetry.endpoint` and
`telemetry.bearer_token_file` are configured. The bearer is read from the file
for each send so the runtime can rotate it without placing secret values in the
router configuration.

Events are sent asynchronously and never contain prompts, response content,
provider credentials, or authorization headers. They contain the request ID,
canonical request type, selected provider/pool/model, fallback state, redacted
attempt outcomes, final HTTP status, latency, token counts when the upstream
returns them, timestamp, streaming state, and `schema_version: 1`. A bounded
local queue (256 events by default) drops new events when full and emits a
rate-limited redacted warning; telemetry delivery failures also produce a
rate-limited redacted warning. Neither condition fails an inference request.

For a container deployment, mount the production template or set
`METAROUTER_CONFIG=/etc/metarouter/production.yaml`, and mount the injected
bearer at `/run/secrets/telemetry_bearer`. The default image
configuration remains the no-secret example profile deliberately.

## Promotion

Production deployment is blocked until this service has passed pre-production smoke tests and the production
configuration uses a non-loopback bind with `server.api_token_env` set. Requests
to API endpoints then require `Authorization: Bearer <token>`.

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run pytest
```
