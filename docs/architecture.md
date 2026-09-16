# Architecture

```text
client → cerberus-api → identity + alias authorization + cost policy
                     → Dispatch → revisioned label binding → provider → response
                     → Fusion backend → OpenRouter /chat/completions
                                      → openrouter:fusion server tool → panel models
                                      → analyst → final response
```

Cerberus owns routing policy, identity authorization, configured free/paid
eligibility, configuration validation and telemetry. A fusion-mode alias is
resolved by Cerberus into a single backend request — the panel, the analyst
(`fusion.judge`), the provider credential and the deadline all come from the
alias — and handed to a `FusionBackend`. The backend performs the deliberation
and returns a normalized result or a normalized failure; Cerberus never
executes a panel itself and no second service is required. The initial backend
is OpenRouter's managed Fusion Router. The panel-size and deadline checks do not
constitute a general billing cap or a global distributed rate limiter.

The backend sees only what Cerberus sends it: a request Cerberus has already
authorized against the caller's identity and alias policy, under the credential
the alias names. A caller cannot select the panel, the analyst, or the tool
surface of a fusion request, and cannot reach the backend except through a
fusion alias it is allowed to use. Fusion-mode deliberation is not streamed.

## Dispatch control plane

A Cerberus alias is a stable, policy-bearing address. The active SQLite revision
defines the complete route universe: service aliases, bindings, ordered paths,
Fusion members, identity policy and credential references. Activation commits
the new current-revision pointer before swapping the validated in-memory
snapshot. Each request captures that snapshot once and records both its revision
and checksum.

Provider health and cooldowns are operational state. They may exclude a member
of the pinned route universe at decision time, but they cannot add or rewrite a
path. Every such exclusion is bounded: a cooldown carries its own `retry_at`, and
a provider-wide `down` carries an expiry, so an exclusion can never outlive the
observation that produced it. A health probe is a point-in-time sample, so it
records `degraded` — never `down` — when the provider cannot be reached; real
per-request failures are excluded at their own scope by the cooldown store.
Normal label lookup reads the in-memory snapshot and performs no SQL.
Routing and usage records are written asynchronously after a decision.

`/health` is the only ungated endpoint and answers liveness only —
`{"status": "ok", "service": "cerberus"}` — which is what a container or
orchestrator probe needs. Operational diagnostics (config version and checksum,
control-plane revision, telemetry delivery health, the cooldown snapshot) live at
`/admin/health`, behind the same read-only admin boundary as `/admin/status` and
`/admin/providers`: the cooldown snapshot names provider, credential and model
for every cooled target, so it is routing topology and never anonymous.

The gateway's static admin console uses guarded admin endpoints. `/admin/validate`,
`/admin/activate` and `/admin/shadow` read a candidate configuration from a path in
the request body, so that path is confined: it is canonicalized and must resolve
inside the staging directory, the directory holding the configuration the process
booted from, or a root named explicitly in `server.admin_config_roots` (empty by
default). Containment is decided on the resolved target, so a `..` segment or a
symlink pointing out of a root is refused before the file is opened. Failures
answer with a stable reason — `outside_allowed_path`, `not_readable`,
`invalid_yaml`, `schema_invalid`, `config_rejected`. The durable invariant is
that no raw source text, parser excerpt, pydantic `input_value` or file content
reaches the caller; candidate-derived identifiers and configured paths may still
appear inside Cerberus-generated policy messages, which is what makes a failure
actionable. Full detail is logged.
Cerberus always boots with an active revision and routes from it, so "a revision
is active" cannot distinguish a gateway an operator has taken charge of from one
still running the configuration it started with. `/admin/status` answers that
separately with `operator_activated`, derived from the audit trail activation
already writes — seeding an empty control plane records `bootstrap`, an explicit
activation records `activate`. It is a read of persisted state, not a new column
and not a flag held in memory, so it survives restart. Without a control-plane
database (`state.path` unset) nothing persists and the answer is necessarily
process-local. The console derives its first-run experience from that field and
from the event ring, never from a stored lifecycle flag: first run is about
establishing the first operator-managed revision, not about a gateway that
cannot serve.

Staging is for edits. `apply_updates` bumps the version only when something
changed, so staging an empty edit would write a re-serialized copy of the active
document under its own version — a candidate `/admin/validate` must then refuse,
because that version is permanently bound to its first checksum. `/admin/config/stage`
answers `409 no_changes` instead; an unchanged configuration is validated and
activated by its own path.

Optional OIDC integrates with a configured identity provider. Telemetry delivery is best effort;
delivery failures do not turn successful inference into a failure. Config activation
is persisted atomically and restart restores the active revision. The rollback stack
is process-local, so restart does not recreate prior rollback depth.

For 0.2.0, deploy one gateway process per routing domain. The SQLite session store
supports shared sessions on one host, but this does not make policy activation or
cooldown updates safe for a multi-process or multi-host gateway deployment.
