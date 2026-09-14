# Persistence review for 0.01

SQLite stores provider/credential/model cooldown deadlines and reasons. With OIDC
enabled it also stores pending login state, nonce, PKCE verifier, session IDs,
identity details, groups and expiry times. Credential columns in cooldown rows
are configuration labels, not provider API-key values. Session data is sensitive.
SQLite is not the configuration authority or a durable routing audit log.

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

Schema setup uses `CREATE TABLE IF NOT EXISTS`; there is no schema-version table,
upgrade framework, automatic corruption recovery or backup command. Opening an
invalid database raises an error; the service does not silently fall back to an
empty store. Preserve the damaged file for investigation and restore a validated
backup. Session expiry does not provide encrypted storage.

The Compose named volume at `/var/lib/cerberus` survives ordinary container
replacement; `down -v` deletes it. This does not depend on a host-specific bind
mount. Native `var/` state depends on the launch directory. A null state path uses
process memory and loses cooldowns, sessions and pending logins at restart.

Back up with SQLite's online-backup mechanism or stop the service and capture a
consistent database/WAL set. Do not copy only the main file during active WAL
writes. Keep config files and secret-manager recovery separately; in-memory
activation/rollback history is not restored by a database backup. Treat restored
session material as sensitive and consider invalidating sessions after recovery.
