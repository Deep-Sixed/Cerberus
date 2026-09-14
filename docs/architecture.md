# Architecture

```text
client → cerberus-api → identity + alias authorization + cost policy
                     → Dispatch / Free Router → provider → response
                     → Fusion worker → model panel → judge → response
```

Cerberus owns routing policy, identity authorization, configured free/paid
eligibility, configuration validation and telemetry. Fusion owns execution of a
configured panel and judge, with panel-size and deadline limits. These checks do
not constitute a general billing cap or a global distributed rate limiter.

The worker uses provider credentials directly. It trusts the gateway boundary;
it does not independently implement the gateway's caller-specific alias policy.
Its bearer token and network must be private to the gateway deployment. A caller
with direct worker access and its token could bypass the gateway's authorization.

The gateway's static admin console uses guarded admin endpoints. Optional OIDC
integrates with a configured identity provider. Telemetry delivery is best effort;
delivery failures do not turn successful inference into a failure. Config activation
and rollback are process-local and restart reloads the configured boot file.

For 0.01, deploy one gateway process per routing domain. The SQLite session store
supports shared sessions on one host, but this does not make policy activation or
cooldown updates safe for a multi-process or multi-host gateway deployment.
