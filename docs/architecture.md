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
path. Normal label lookup reads the in-memory snapshot and performs no SQL.
Routing and usage records are written asynchronously after a decision.

The gateway's static admin console uses guarded admin endpoints. Optional OIDC
integrates with a configured identity provider. Telemetry delivery is best effort;
delivery failures do not turn successful inference into a failure. Config activation
is persisted atomically and restart restores the active revision. The rollback stack
is process-local, so restart does not recreate prior rollback depth.

For 0.01, deploy one gateway process per routing domain. The SQLite session store
supports shared sessions on one host, but this does not make policy activation or
cooldown updates safe for a multi-process or multi-host gateway deployment.
