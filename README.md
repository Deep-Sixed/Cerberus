# Cerberus 0.01

Cerberus is a policy gateway with an OpenAI-compatible chat-completions API,
identity-based routing, free-only selection, configuration management, an admin
console, and Fusion panel-and-judge integration.

Lineage: **MetaRouter v3 → MetaRouter v4 / Cerberus**. The public product version
is **0.01**. MetaRouter v4 describes ancestry, not the release number. Python
packaging normalizes `0.01` to `0.1` in distribution metadata and filenames;
release names and this product's version declaration remain `0.01`.
See [Python version normalization](https://packaging.python.org/en/latest/specifications/version-specifiers/#integer-normalization).

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

## Container and Fusion

`Containerfile` builds the gateway. `deploy/fusion/compose.yaml` describes the
API/worker boundary. To prepare the bundle:

```sh
cp .env.example .env  # populate locally with your credentials and cb- caller keys
uv run --frozen cerberus fusion-config --config config/fusion-dev.yaml --out worker.generated.yaml
docker compose --env-file .env -f deploy/fusion/compose.yaml config --quiet
docker compose --env-file .env -f deploy/fusion/compose.yaml up --build -d
```

Port 4000 is a loopback-only example binding; choose an unused port if needed.
See [Fusion provenance](docs/fusion-provenance.md) for the
worker's pinned source and outstanding build constraints. Provider model names
in example configurations are illustrative; check availability and cost with
your provider before sending requests.

The API is the client entry point. The worker receives authenticated gateway
work and must remain on a dedicated network without a published host port.
See [architecture](docs/architecture.md), [persistence](docs/persistence.md),
and [security](SECURITY.md).

## Release status

This is a prepared 0.01 public-release candidate, not a production-readiness
claim. Public release remains gated on a clean retained-history audit and the
validation/provenance results. Host deployment records, production identities,
secret files and incident runbooks belong outside this product repository.

Cerberus is MIT licensed; see `LICENSE` and `NOTICE` for attribution.
