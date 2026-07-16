# Cerberus

Unified policy routing service for the EVECOR gateway — **three heads, one body**.

```text
                         CERBERUS
              ┌────────── Dispatch ──────────┐
Clients ──────┼────────── Free Router ───────┼── Providers
              └────────── Fusion ────────────┘
                 Shared registry · identity · selection engine
                 state · config lifecycle · telemetry · admin UI
```

Cerberus replaces MetaRouter v3: one OpenAI-compatible endpoint (port 4000 at
cutover), one configuration tree, one identity system, one deterministic selection engine,
one telemetry stream, one read-only admin dashboard, plus a bundled fusion worker
(`cerberus-fusion-worker`) for multi-model deliberation.

- **Dispatch** — deterministic identity-to-model assignment (ordered candidates per alias).
- **Free Router** — free-only routing; no implicit free→paid fallback, ever.
- **Fusion** — panel + judge deliberation; policy here, execution in the bundled worker.

## Documents

| File | Purpose |
|---|---|
| [SPEC.md](SPEC.md) | Frozen specification (2026-07-16) — the scope contract for every session |
| [PLAN.md](PLAN.md) | 15-session build sequence, acceptance tests, review gates |

## Status

Phase 0 complete: donor code imported (MetaRouter v3, first commit), package restructured.
The donor modules (`app.py`, `config.py`, `routing.py`, `cli.py`, `telemetry/emitter.py`)
are v3 code renamed — they run and their tests pass, but Sessions 1–3 rewrite config,
selection, and state onto the Cerberus schema. Subpackage `__init__.py` docstrings carry
each module's charter.

## Development

```bash
uv sync                  # env (Python 3.14.5)
./scripts/ci.sh          # ruff + pytest — must be green at end of every session
```

Keep deployment configuration separate from product source (compose + config +
file-secrets only — no source in the gateway tree). Secrets are references (environment /
Docker file-secrets); no secret values in code, config, tests, or logs.
