# Persistence review for 0.01

SQLite stores two state classes. Revisioned control state contains service
aliases, bindings, ordered paths, Fusion members, identity policy and credential
references plus one atomic active-revision pointer. Operational state contains
provider health, cooldowns, routing events, usage projections and audit records;
with OIDC enabled it also includes pending login and session state. Credential
columns are reference names, never provider API-key or client-token values.

The session store uses WAL, a five-second busy timeout and atomic
`DELETE ... RETURNING` to consume a login state once. Tests cover two store
connections and app instances plus restart recovery. These do not prove general
multi-worker routing consistency: the cooldown store checks then writes in
separate statements, so competing writers can shorten an existing deadline.
Expiry cleanup also selects then deletes without a conditional expiry predicate.
The smallest safe release restriction is one gateway process per routing domain.
Before supporting multiple writers, use conditional atomic UPSERT/deletion and
add real concurrent-process tests. Multi-host policy/state coordination needs a
separate design; merely replacing SQLite would not solve process-local activation.

Schema setup uses `CREATE TABLE IF NOT EXISTS` and records control schema version
1; there is not yet an upgrade runner, automatic corruption recovery or backup
command. `state.path` is the boot-time control-plane locator and cannot change
between revisions stored in that database. Opening an invalid database raises an
error; the service does not silently fall back to an empty store. Preserve the
damaged file for investigation and restore a validated backup. Session expiry
does not provide encrypted storage.

The Compose named volume at `/var/lib/cerberus` survives ordinary container
replacement; `down -v` deletes it. This does not depend on a host-specific bind
mount. Native `var/` state depends on the launch directory. A null state path uses
process memory and loses cooldowns, sessions, pending logins, revision history
and durable routing records at restart.

Back up with SQLite's online-backup mechanism or stop the service and capture a
consistent database/WAL set. Do not copy only the main file during active WAL
writes. Keep authoring files and secret-manager recovery separately. The active
revision and materialized catalog are restored from SQLite; the in-process
rollback stack is not. Treat restored session material as sensitive and consider
invalidating sessions after recovery.
