# Cerberus

Cerberus is an AI service router with stable logical addressing over changing
providers, models and composed inference paths. Its OpenAI-compatible API exposes
policy-bearing aliases for dedicated services and Fusion routes; clients bind to
those aliases without depending on provider topology.

## Development

Use Python **3.14.5** and uv **0.11.29**. The committed `uv.lock` pins dependency
resolution; CI installs it without updating it.

```sh
uv sync --frozen --python 3.14.5
./scripts/ci.sh
uv build --no-build-isolation
```

The admin UI is plain HTML, CSS and JavaScript shipped in the wheel; there is no
separate frontend compilation step. JavaScript syntax is checked with Node 26.3.1.

For a local OpenAI-compatible upstream listening on port 8080:

```sh
export LOCAL_API_KEY=local-no-auth
uv run --frozen cerberus serve --config config/local.example.yaml
```

This example serves on loopback port 4111. Replace the model identifier and
credential for your upstream. The example placeholder is only for servers that
have authentication disabled; it is not a production credential.

## Container

`Containerfile` builds the gateway; it is the only application service.
`deploy/fusion/compose.yaml` runs it with the fusion-enabled dev config:

```sh
cp .env.example .env  # populate locally with your credentials and cb- caller keys
docker compose --env-file .env -f deploy/fusion/compose.yaml config --quiet
docker compose --env-file .env -f deploy/fusion/compose.yaml up --build -d
```

Port 4000 is a loopback-only example binding; choose an unused port if needed.
Provider model names in example configurations are illustrative; check
availability and cost with your provider before sending requests.

## Fusion

Fusion is an external integration, not vendored code. Cerberus owns the policy —
which identities may call a fusion alias, which models form the panel, which
model acts as the analyst (`fusion.judge`), the cost tier, and the deadline —
and hands the deliberation itself to a managed backend selected by
`fusion.backend`. The initial backend is
[OpenRouter's Fusion Router](https://openrouter.ai/docs/guides/routing/routers/fusion-router):
each fusion request becomes one OpenRouter chat-completions call whose outer
model is validated by Cerberus policy and whose `openrouter:fusion` server tool
names the panel (`analysis_models`) and analyst (`model`). `tool_choice:
required` forces the deliberation on every fusion request. Cerberus reports the
one call it made — request id, alias, panel, analyst, outer model, returned
model, OpenRouter generation id, usage, latency — and never fabricates per-seat
detail the backend does not expose.

Requirements and caveats:

- An OpenRouter API key is required for that backend; every panel candidate and
  the judge must use the same provider credential.
- A fusion request incurs multiple model calls and is billed as their sum.
- OpenRouter Fusion is an evolving external API; its behavior and limits are
  OpenRouter's, not Cerberus's.
- Backend failures fail closed for fusion aliases only; dispatch and free
  aliases keep working.
- Per-seat persona prompts from the earlier bundled worker are not available;
  the panel answers the caller's prompt directly.

## Dispatch

Dispatch resolves a stable Cerberus alias through a validated, identity-scoped
route revision. Dedicated aliases forward to an ordered provider/model path;
Fusion aliases resolve to a configured multi-model composition. Health and
cooldowns may remove configured paths from eligibility, but can never introduce
a path absent from the request's pinned revision.

When `state.path` is configured, validated revisions are materialized in SQLite
and activation updates one atomic current-revision pointer. Requests read an
immutable in-memory snapshot; routing events and usage projections are persisted
as operational records without storing provider or client secret values.

See [architecture](docs/architecture.md), [persistence](docs/persistence.md),
and [security](SECURITY.md).

## Release status

A tagged release is not a production-readiness claim. Publication requires
operator review of the final sanitized-history and validation/provenance
report. Host deployment records, production identities, secret files and
incident runbooks belong outside this product repository.

Cerberus is MIT licensed; see `LICENSE` and `NOTICE` for attribution.
