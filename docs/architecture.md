# Architecture

```text
client → cerberus-api → identity + alias authorization + cost policy
                     → Dispatch → revisioned label binding → provider → response
                     → Fusion backend → OpenRouter /chat/completions
                                      → openrouter:fusion server tool → panel models
                                      → analyst → final response
                     → filtered pool → Jev Router → OpenRouter /chat/completions
                                      → typesafe/jev-router chooses one pool model
                                      → decision verified against the pool → response
                     → filtered pool → Jev decision → OpenRouter /api/alpha/decisions
                                      → chosen model first → Dispatch → any provider
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

## Jev Router

A `jev-router` alias makes Jev one model-selection strategy beside dedicated
paths and Fusion; it does not hand Cerberus's policy to it. Cerberus answers
"what may run?" and Jev answers "which of those should run?" for one request.

The pool is the alias's candidates, each an exact registry slug under one
OpenRouter credential. Before every request `router/pool.py` filters it in the
failover loop's own order — cost (a paid member needs `allow_paid_pool`), then
provider health, the most specific active cooldown, and credential presence —
and only the survivors are sent, as the `jev-router` plugin's `models` list on a
`typesafe/jev-router` call. An empty pool is never sent: the plugin treats an
include list that matches nothing as no list and routes over OpenRouter's whole
pool, so Cerberus answers 503 instead. Patterns (`*`, `~family`) are refused at
validation for the same reason.

Every call asks for router metadata, and the response is accepted only when the
`jev-router` stage is present, reports no `list_fallback`, and names a served
model and resolved models that are all in the pool sent (a slug also matches its
`-YYYYMMDD` revisions, as the plugin documents). Anything else — `models_ignored`
above all — is withheld with 502 and recorded as `out_of_policy`. The call has
already run and been billed, so its usage and reported cost are still recorded.
The metadata is removed from the payload; the router's decision is reported in
the `cerberus` envelope and the `jev_router` telemetry record instead.

A jev-router request accepts an allowlist of caller fields, not a denylist:
messages, sampling and output controls, and function tools. Anything else is
refused with 400 before any upstream call, including model fallback lists,
`route`, `plugins`, `preset`, `provider`, server tools and any field this
release does not know. `reasoning_effort` and `reasoning` are refused because
Jev chooses the effort; a per-candidate reasoning budget is refused at
validation for the same reason. Jev Router requests are not streamed, because
the decision has to be checked before any of the answer reaches the caller.

`/admin/routes` projects the alias as a pool with no preference order: each
member is `eligible` (in the pool the next request would send) or `excluded`
with the first filter that refused it, from the same `resolve_pool` dispatch
calls. Readiness is the backend's presence and a non-empty pool.

The hosted router can only choose among models one OpenRouter credential
reaches; it cannot place a request on a local or non-OpenRouter provider. A
`jev` alias does. There is no per-request cost ceiling: Cerberus does not hold
model prices, and a ceiling checked after a routed call is too late.

## Jev

A `jev` alias uses Jev as Cerberus's model-selection intelligence and keeps
execution in Cerberus. Three layers, three questions: Cerberus answers "what may
run?", Jev answers "what should run?", and the provider runs it.

The pool is the alias's candidates on any provider — a local llama.cpp host,
Cloudflare, OpenRouter — each a registry model with a `strength`. Before every
request `router/pool.py` filters it exactly as it does for jev-router (cost, then
health, cooldown, credential). The distinct models that survive are offered to
Jev under opaque ids (`m1`, `m2`, …) and described by registry facts only: model
id, strength, cost tier, context window, capabilities, description. Provider and
credential names never leave. One `choice` question goes to OpenRouter's
Decisions API (`POST /api/alpha/decisions`, model `typesafe/jev-1.13` pinned by
default) under the decider credential the policy names: which candidate is the
least costly one still strong enough for this request.

The decision service is a third party even when the winner is local, so what it
reads is policy (`jev.input`): the latest user message (default), the whole
conversation, or request metadata with no prompt text at all — always truncated
to `max_input_chars`, always with the request's shape (turns, size, tools,
response format, requested output length). With one model or none left there is
nothing to choose, and no decision is asked, so no text leaves.

Jev's answer can only reorder: the chosen model goes first, the rest of the
cost-admitted pool follows in configured order, and the ordinary failover loop
executes that order — health, cooldown and credential gates, 429 cooldowns,
streaming and telemetry included. An answer that is not exactly an offered id is
never interpreted. Every way of not getting a usable answer — no decider backend
or credential, the decider's provider marked down, timeout, transport or HTTP
error, an unreadable response, an absent or unknown choice — runs the request in
configured order instead, which every pool member already satisfies; the alias
stays routable while no decision can be had.

With `jev.reasoning_efforts` set, the same call asks a second question: how much
reasoning effort the request needs, answered from that list only. The answer
replaces the configured `reasoning_effort` of every planned candidate that sets
one, and is never added to a candidate that sets none, because a route without
one may not accept the parameter at all; the list is how an operator keeps Jev
to values every such route accepts. The egress rule is unchanged: a caller's own
stated effort wins unless the candidate sets `reasoning_effort_override`. The
effort is vetted on its own — an answer outside the list leaves the configured
effort standing, whatever the model answer was — and with one model left the
call is still made for the effort alone. A plan may change the effort of a
target that sets one and nothing else about it; the dispatch loop refuses any
other difference from a configured target.

Each routing event carries a `jev` record: options offered, whether the decision
was `chosen`, `skipped` or a `fallback`, the reason, the choice and its
probability, the effort applied with its probability or why none was, decision
latency, status and usage. It never carries request text.
The confirmed Decisions API contract covers the endpoint, the model, the
`model`/`state`/`questions` request and `answers` keyed by question id; the
fields inside one question and one answer are not yet confirmed, so the answer
is read tolerantly and a misread degrades to configured order rather than to an
unvetted model.

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

The request body cannot add a path either. On every alias the failover loop
serves (`dispatch`, `free`, `jev`, and direct free models), the body a provider
receives is the caller's restricted to the standard chat-completions fields in
`caller_fields.CALLER_FIELDS`, with `model` replaced by the target being
attempted. Every other field is dropped before the upstream call, without
error, as most gateways do. That includes OpenRouter's `models` fallback list,
`route`, `plugins`, `preset` and `provider`, because each can select or add a
model the pinned revision never named, or the cost gate never weighed, under
the operator's credential. Function tools are forwarded; a `tools` entry or a
`tool_choice` of any other type is refused with 400 (`reason: unsupported_tool`).
Such a tool is a server tool that runs upstream: `openrouter:fusion` runs a
panel of its own. The refusal is recorded as a routing event with outcome
`invalid_request`, under the caller's identity and the request's revision, and
the response carries the event's `request_id`, so a refused request appears in
`/admin/events` beside every other routing decision. Fusion builds its backend
body from the same allowlist and refuses caller `tools`, `tool_choice` and
`plugins` outright, also with a 400 and a routing event. Jev Router refuses
rather than drops every field outside its own allowlist (see Jev Router).

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

`/admin/routes` answers what the router would do with the active revision,
resolved server-side. The console must not decide whether a route path is
policy-eligible, whether paid fallback applies, whether health or a cooldown
excludes a provider, or how a fusion chain resolves — a browser that re-derived
those would be a second router, free to disagree with the real one. The
projection reports the router's own verdicts instead: `engine.cost_eligible` for
the cost gate, `availability.runtime_exclusion` for health, cooldown and
credential presence in the failover loop's order, `auth.authorization_error` for
each configured identity, and `fusion.fusion_readiness` for a fusion alias's
three gates. A path is `eligible` when the router would attempt it first,
`standby` when it is attemptable but a later preference, and `excluded` when the
router would skip it before any upstream request — carrying the first reason the
loop would reach, so a cost-prohibited path reports the prohibition even when it
is also cooled down. Fusion is projected as a service chain (panel, analyst,
resolved outer model) with one readiness verdict, because its panel never enters
the failover loop and labelling members eligible would describe a loop that does
not run. The endpoint names credential references but never a secret value or
the environment variable one is read from, opens no upstream connection, and
applies no cooldown.

`/admin/audit` answers what changed about the configuration: the control plane's
own registration, bootstrap, activation and rollback records, newest first and
read-only. Nothing writes, deletes or acknowledges a record, and the endpoint
adds no table and no migration — it reads the `audit_records` rows `activate()`
has always written, and `operator_activated` already derives from. `detail_json`
is parsed server-side and returned as `detail`; the stored column is never handed
over raw. The read is bounded (`?limit=`, default 100, clamped to 1..500) because
the table is lifetime history; there is no cursor, which is a thing to design when
something needs to walk further back than that window. A deployment with no
control-plane database answers `{"persistent": false, "records": []}` rather than
implying it keeps durable history.

Every field on that surface is bounded by construction, which is why it can be
exposed at all: `action` is one of the four literals `_audit` is called with,
`revision` is a config version the schema constrains to `cerberus-YYYY-MM-DD.N`,
`checksum` is a digest, `occurred_at` is a timestamp, and `detail` is either `{}`
or `{"activated_at": …}`. No credential, environment-variable name, candidate
file content or filesystem path is stored, so none can be read back.

Audit and the routing ring are different surfaces and stay that way. Audit is
persisted control and configuration lifecycle; `/admin/events` is ephemeral
per-process routing history. Both carry timestamps, which is not a reason to
merge them.

The order those runtime gates run in lives in `router/availability.py` and is
read from there by both the dispatch loop and the projection. Two copies would
be free to drift, and the console would then describe a skip the router does not
make.

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

For 0.3.0, deploy one gateway process per routing domain. The SQLite session store
supports shared sessions on one host, but this does not make policy activation or
cooldown updates safe for a multi-process or multi-host gateway deployment.
